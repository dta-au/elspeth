# Tool-Result Envelope Explore-and-Pin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the freeform composer tool-result seam so every key `ToolResult.to_dict()` ships to the planner is derived from one registry, admitted by the redaction manifest from that same registry, and either taught to the model in the skill or tool descriptions or fenced with a ratified reason, with a whole-tree gate that turns red on any future drift.

**Architecture:** One leaf module becomes the single authority for the envelope key vocabulary and the guidance TypedDicts; `ToolResult`, the redaction manifest, and the planner's closed discovery twin all derive from it. A new whole-tree gate derives the shipped side from the AST of the producers and the owned TypedDicts, the admitted side from the live manifest objects, and the taught side from the rendered system prompt plus every `ToolDeclaration.description`, then joins them into a matrix. The loose `Mapping[str, Any]` fields on `ToolResult` become closed types with nominal runtime admission (ADR-032), so the gate is a backstop rather than the fix.

**Tech Stack:** Python 3.12, `ast`, `typing.get_type_hints`, pytest (`-n 0` for single tests), pydantic v2 (redaction shadow models), mypy, ruff, `elspeth-lints` trust-tier gate (shape-only verify mode), the composer web API for the live trial.

**Spec:** `docs/agents/explore-and-pin-methodology.md` (the method) and ticket **elspeth-e405ad7cd2** (the seam). The three exploration reports that ground every citation below live in the lane scratch directory as `explore-producer.md`, `explore-consumer.md`, `explore-teaching.md`; they are ticket evidence, not repo files.

## Global Constraints

- Worktree: `.claude/worktrees/lane-strany-toolresult`, branch `strany/tool-result-envelope`, base `release/0.8.0` @ `d14dd221f`. Every command starts with `cd $W &&`, where `$W` is the worktree's absolute path (resolve it once with `git rev-parse --show-toplevel` from inside the worktree; never write the absolute path into a tracked file).
- Run tests with `PYTHONPATH=$W/src:$W/elspeth-lints/src $W/.venv/bin/python -m pytest` and check `elspeth.__file__` points into the worktree once per shell (AGENTS.md worktree gotcha).
- Composer invariants: no server-authored pipeline structure, no tutorial-special path. Skill prose and tool descriptions are teaching, which is allowed; nothing in this plan routes around the provider.
- Never add `# noqa`, `# type: ignore`, or a lint suppression. Never hand-edit a `judge_metadata_signature`. Never hold the judge HMAC key.
- Never `git stash`; never `git checkout -- <path>` to restore a mutation (cp round-trip only).
- Full suites run to a lane-private log under `setsid nohup` with a done marker; report the exit code, never a `tail`.
- Lint corpus is compared to the base commit's corpus, never to zero.
- No ratification is the agent's. Phase 3 (Task 6) is a hard stop until John has ratified per row.
- Claims are measured: every count in the ticket comes from the gate's census functions, not from grep.

---

## Seam definition (what the gate will hold together)

| Side | Authority today | After this plan |
|---|---|---|
| Shipped | `ToolResult.to_dict()` literal dict (`tools/_common.py:958-999`) plus the helpers it calls (`_compute_validation_delta` `:657`, `_applied_component_echo` `:3715`, `build_validation_guidance` `generation.py:1706`, `_semantic_contracts_payload` `:628`, `_graph_repair_suggestions` `:868`) and the `data` payload each producer passes | same code, but the top-level and sub-envelope keys are pinned equal to the registry by the gate |
| Admitted | `_ToolResultResponseModel` (`redaction.py:3345`), `_TOOL_RESULT_REQUIRED/OPTIONAL_RESPONSE_KEYS` + `_tool_result_response_keys` (`:3561-3582`), `_TOOL_RESULT_ENVELOPE_KEYS` (`:92`), per-tool `known_response_keys` | the tuples import from the registry; the model's fields are pinned equal to the registry by the gate; `affected_nodes` joins the implicit envelope set (decision D1) |
| Taught | `skills/pipeline_composer.md`, `skills/pipeline_capabilities.md` (rendered by `prompts.build_system_prompt`), and every `ToolDeclaration.description` | a "Reading a tool result" section teaches each ratified key in quoted form; descriptions name their tool-specific `data` keys |

Surfaces the census enumerates (the `surface` column of every row):

| surface | keys under | shipped-side derivation |
|---|---|---|
| `envelope` | top level | AST of `to_dict` |
| `validation` | `validation.*` | AST of the nested literal + `_typed_keys(ValidationEntryDict)`, `_typed_keys(_SemanticEdgeContractPayload)`, `_typed_keys(_GraphRepairSuggestion)` |
| `delta` | `validation_delta.*` | AST of `_compute_validation_delta`'s return literal (entries reuse `ValidationEntryDict`) |
| `guidance` | `validation_guidance.*` | `_typed_keys(ValidationGuidance)` |
| `echo` | `applied_component.*` | AST of `echo[...] =` in `_applied_component_echo`; nested node/output/edge/source keys are taught by the `set_pipeline` argument schema, not prose |
| `failure-data` | `data.*` from shared helpers | AST literals in `_failure_result`, `_credential_wiring_contract_failure`, `_merged_component_rejection_result`, `tool_batch.run_tool_batch` (proposal and prevalidation dicts) |
| `tool-data` | `data.*` per tool | the `data=` argument at each `_discovery_result` / `_mutation_result` call: literal keys, or an owned TypedDict / pydantic model, or an allowlisted helper mapped to its payload type; anything else is a walker refusal |

Facts that shape the verdicts (from the exploration reports):

- The model receives `json.dumps(result.to_dict())` verbatim (`discovery_cache.py:44-46`); redaction runs only at persist (`turn_audit.py:236-251`, `audit_storage.py:179`). Untaught keys mislead the planner; unadmitted keys break or blind the audit row.
- On the ten type-driven mutation tools a new top-level key raises `pydantic.ValidationError` at persist time, uncaught (`redaction.py:4103`, `extra="forbid"`). On `upsert_node`/`set_output`/`set_metadata` it silently collapses to `_unknown_response`. On the 26 `handles_no_sensitive_data=True` tools it is indistinguishable from today.
- `affected_nodes` is a required producer key absent from `_TOOL_RESULT_ENVELOPE_KEYS`, so all 26 declarative discovery-style tools fire `unknown_response_key_redacted` on every call. The drift counter is permanently non-zero.
- `_tool_result_response_keys` and `_ToolResultResponseModel` are two hand-maintained copies of the vocabulary with no cross-check (no test references either name).
- Taught today in quoted form: `applied_component`, `validation`, `validation_delta`, `plugin_schemas`. `validation_guidance` and all of its sub-keys are taught nowhere. `docs/reference/composer-tools.md:530-558` is stale operator prose (not model-facing).
- Prior adjudication: the shallow declarative redaction bug (elspeth-dcbf4389dc, duplicate elspeth-404ef3de28) closed 2026-06-30 as fixed by `_redact_declarative_known_response_value`; treat nested redaction as settled.

## Decisions John ratifies (collected in Task 6, not before)

- **D1** Add `affected_nodes` to `_TOOL_RESULT_ENVELOPE_KEYS` so declarative rows admit it through the same untrusted-structure projection the type-driven path applies (node ids become text sentinels either way). Recommendation: yes. Retires the always-on telemetry noise and aligns the two paths; changes no persisted secret.
- **D2** `ToolResult.data` field type. Recommendation: `Mapping[str, object] | Sequence[object] | BaseModel | None` with nominal runtime admission (exact `dict`/`MappingProxyType`/`list`/`tuple` or a pydantic `BaseModel`), leaving per-tool `data` shapes to their existing TypedDicts. `Any` goes; `object` still forces narrowing at every consumer.
- **D3** Scope of the `tool-data` surface. Recommendation: census and matrix it in full; verdict rule is "taught by the tool's own description, or by the `set_pipeline` argument schema when the key is authoring vocabulary the model itself wrote". If the untaught `tool-data` rows exceed the shared-surface rows, land the shared surfaces in this lane and open one sibling ticket under the strany epic carrying the `tool-data` census verbatim, with the fence fixture holding each deferred row under a reason that names that ticket. That is a fence with a checkable reason, not a parking spot.
- **D4** The 26 `handles_no_sensitive_data=True` tools persist no `data` at all (pinned by `test_tool_result_envelope_keys_are_implicitly_known_for_declarative_entries`). The audit row therefore cannot reproduce what the model read from `list_sources` or `get_pipeline_state`. Recommendation: no change in this lane; it is redaction doctrine, raised so John can decide whether a later lane admits closed-vocabulary discovery payloads.
- **D5** `diff_pipeline` returns `success=True` with `data.error` when no baseline exists (`generation.py:3944`). Recommendation: producer fix, `success=False` through `_failure_result` with a closed `error_code`, so `error` is a failure-only key on every surface.

---

## File structure

| Path | Responsibility |
|---|---|
| Create `src/elspeth/web/composer/tool_result_envelope.py` | Leaf module, imports nothing from `elspeth.web.composer`: the envelope key registry tuples and the `ValidationGuidance` / `ValidationCodeGuidance` TypedDicts. (`tools/__init__.py` imports `_dispatch`, which imports `redaction`, so the leaf cannot live under `tools/`.) |
| Modify `src/elspeth/web/composer/tools/_common.py` | `ToolResult` field types, `AppliedComponentEcho` TypedDict, runtime admission, `to_dict` unchanged in shape |
| Modify `src/elspeth/web/composer/tools/generation.py` | re-export the guidance TypedDicts from the leaf; `explain_tool` text names its container; D5 fix |
| Modify `src/elspeth/web/composer/redaction.py` | key tuples import from the leaf; `affected_nodes` in the implicit envelope set (D1) |
| Modify `src/elspeth/web/composer/pipeline_planner.py` | no code change expected; the gate pins `_ClosedProviderDiscoveryPayload` keys ⊆ registry |
| Modify `src/elspeth/web/composer/skills/pipeline_composer.md` | new section "Reading a tool result" |
| Modify per-tool declarations (`tools/sessions.py`, `generation.py`, `sources.py`, `transforms.py`, `blobs.py`, `secrets.py`, `outputs.py`) | descriptions name their `data` keys per verdict |
| Create `tests/unit/web/composer/_teaching_gate_support.py` | walker helpers shared by both teaching gates (`_typed_keys`, `_is_cast`, `_is_dict_literal`, `_literal_str`, `composer_python_files`, `_display`, `is_quoted_leaf`) |
| Modify `tests/unit/web/composer/test_planner_teaching_gate.py` | import the shared helpers instead of defining them; behaviour unchanged |
| Create `tests/unit/web/composer/test_tool_result_envelope_gate.py` | the new gate: shipped / admitted / taught derivations, walker probes, matrix, four fence tests, admitted-side equality tests |
| Create `tests/unit/web/composer/tool_result_envelope_fence.json` | fence fixture, same four-field entries as `planner_teaching_fence.json` |
| Modify `tests/unit/web/composer/test_redact_tool_call_response.py`, `test_declarative_manifest_runtime_smoke.py` | D1 pins |
| Modify `tests/unit/web/composer/test_tool_common.py` (or the nearest `ToolResult` unit file) | field-type admission tests |
| Modify `docs/reference/composer-tools.md` | "Tool Result Format" regenerated from the registry (operator prose, corrected) |

---

### Task 0: Baselines and lane bookkeeping

**Files:**
- No repo edits. Outputs go to `$S` = the lane scratch directory `.../scratchpad/lane-strany-toolresult/`.

- [ ] **Step 1: Pin the shell variables and verify import roots**

```bash
W=$(git rev-parse --show-toplevel)   # run from inside the worktree; export W for the census script
S=<the session scratchpad directory>/lane-strany-toolresult                                           # lane-private; never a shared filename
cd $W && PYTHONPATH=$W/src:$W/elspeth-lints/src $W/.venv/bin/python -c "import elspeth, elspeth_lints; print(elspeth.__file__); print(elspeth_lints.__file__)"
```
Expected: both paths under `$W`.

- [ ] **Step 2: Baseline the lint corpus on the files this lane will touch, from the base commit's tree**

```bash
cd $W && BASE=$(git rev-parse HEAD) && mkdir -p $S/base && git archive $BASE src/elspeth/web/composer | tar -x -C $S/base
cd $S/base && ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing PYTHONPATH=$W/src:$W/elspeth-lints/src $W/.venv/bin/python -m elspeth_lints check --rules all --root $S/base/src/elspeth > $S/lint-base.log 2>&1; echo "exit=$?"
grep -c "^[^ ].*: R" $S/lint-base.log
```
Record the count and the exit code in `$S/baselines.md`. Exit 1 with findings is the expected fail-closed state.

- [ ] **Step 3: Baseline mypy on the composer package**

```bash
cd $W && PYTHONPATH=$W/src:$W/elspeth-lints/src $W/.venv/bin/python -m mypy src/elspeth/web/composer > $S/mypy-base.log 2>&1; echo "exit=$?"; tail -1 $S/mypy-base.log
```

- [ ] **Step 4: Baseline the two gates that this lane will move**

```bash
cd $W && PYTHONPATH=$W/src:$W/elspeth-lints/src $W/.venv/bin/python -m pytest tests/unit/web/composer/test_planner_teaching_gate.py tests/unit/web/composer/test_redact_tool_call_response.py tests/unit/web/composer/test_adequacy_guard.py -n 0 -q > $S/gates-base.log 2>&1; echo "exit=$?"; tail -3 $S/gates-base.log
```
Expected: exit 0.

- [ ] **Step 5: Ticket comment**

Post to elspeth-e405ad7cd2 (actor `claude-fable`): base commit, the three baseline numbers, the paths of the three exploration reports.

---

### Task 1: The envelope registry leaf module

**Files:**
- Create: `src/elspeth/web/composer/tool_result_envelope.py`
- Modify: `src/elspeth/web/composer/tools/generation.py:1684-1704` (re-export)
- Test: `tests/unit/web/composer/test_tool_result_envelope.py`

**Interfaces:**
- Produces: `TOOL_RESULT_REQUIRED_KEYS`, `TOOL_RESULT_OPTIONAL_KEYS`, `TOOL_RESULT_POST_DISPATCH_KEYS`, `VALIDATION_KEYS`, `VALIDATION_DELTA_KEYS`, `APPLIED_COMPONENT_KEYS` (all `Final[tuple[str, ...]]`), `tool_result_keys(*, data: bool) -> tuple[str, ...]`, `ValidationCodeGuidance`, `ValidationGuidance`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/web/composer/test_tool_result_envelope.py
"""The envelope registry is the single authority for the ToolResult wire vocabulary (elspeth-e405ad7cd2)."""

from __future__ import annotations

import ast
from pathlib import Path

from elspeth.web.composer import tool_result_envelope as env

REPO_ROOT = Path(__file__).resolve().parents[4]
LEAF = REPO_ROOT / "src" / "elspeth" / "web" / "composer" / "tool_result_envelope.py"


def test_registry_tuples_are_disjoint_and_ordered() -> None:
    required = env.TOOL_RESULT_REQUIRED_KEYS
    optional = env.TOOL_RESULT_OPTIONAL_KEYS
    assert required == ("success", "validation", "affected_nodes", "version")
    assert optional == ("data", "runtime_preflight", "validation_delta", "post_call_hints", "plugin_schemas", "validation_guidance", "applied_component")
    assert not set(required) & set(optional)
    assert env.TOOL_RESULT_POST_DISPATCH_KEYS == ("pipeline_content_hash_schema", "pipeline_content_hash")


def test_tool_result_keys_drops_data_only_when_asked() -> None:
    with_data = env.tool_result_keys(data=True)
    without = env.tool_result_keys(data=False)
    assert "data" in with_data and "data" not in without
    assert tuple(k for k in with_data if k != "data") == without


def test_leaf_module_imports_nothing_from_the_composer_package() -> None:
    """redaction.py and tools/_common.py both import this module; a composer import here is a cycle."""
    tree = ast.parse(LEAF.read_text(encoding="utf-8"))
    offenders = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("elspeth.web")
    ]
    assert offenders == [], offenders


def test_guidance_typed_dicts_live_in_the_leaf_and_are_reexported() -> None:
    from elspeth.web.composer.tools import generation

    assert generation.ValidationGuidance is env.ValidationGuidance
    assert generation.ValidationCodeGuidance is env.ValidationCodeGuidance
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd $W && PYTHONPATH=$W/src:$W/elspeth-lints/src $W/.venv/bin/python -m pytest tests/unit/web/composer/test_tool_result_envelope.py -n 0 -q
```
Expected: FAIL with `ModuleNotFoundError: elspeth.web.composer.tool_result_envelope`.

- [ ] **Step 3: Write the leaf module**

```python
# src/elspeth/web/composer/tool_result_envelope.py
"""The closed ToolResult wire vocabulary — the one authority for producer, redaction, and planner twins.

``ToolResult.to_dict`` (tools/_common.py) emits exactly these top-level keys;
``redaction._ToolResultResponseModel`` and ``redaction._tool_result_response_keys``
admit exactly these; ``pipeline_planner._ClosedProviderDiscoveryPayload`` is a
subset of these. ``tests/unit/web/composer/test_tool_result_envelope_gate.py``
pins all three against this module from the AST and the live objects, so a key
added in one place and not the others turns the tree red (elspeth-e405ad7cd2).

This module imports nothing from ``elspeth.web``: both ``redaction`` and
``tools/_common`` import it, and ``tools/__init__`` imports ``_dispatch`` which
imports ``redaction``.
"""

from __future__ import annotations

from typing import Final, NotRequired, TypedDict

TOOL_RESULT_REQUIRED_KEYS: Final[tuple[str, ...]] = (
    "success",
    "validation",
    "affected_nodes",
    "version",
)
"""Present on every serialized ToolResult, in emission order."""

TOOL_RESULT_OPTIONAL_KEYS: Final[tuple[str, ...]] = (
    "data",
    "runtime_preflight",
    "validation_delta",
    "post_call_hints",
    "plugin_schemas",
    "validation_guidance",
    "applied_component",
)
"""Emitted only when set / non-empty, in emission order."""

TOOL_RESULT_POST_DISPATCH_KEYS: Final[tuple[str, ...]] = (
    "pipeline_content_hash_schema",
    "pipeline_content_hash",
)
"""Attached after dispatch by pipeline_commit (set_pipeline only); never emitted by to_dict."""

VALIDATION_KEYS: Final[tuple[str, ...]] = (
    "is_valid",
    "errors",
    "warnings",
    "suggestions",
    "semantic_contracts",
    "graph_repair_suggestions",
)

VALIDATION_DELTA_KEYS: Final[tuple[str, ...]] = (
    "new_errors",
    "resolved_errors",
    "new_warnings",
    "resolved_warnings",
)

APPLIED_COMPONENT_KEYS: Final[tuple[str, ...]] = (
    "source",
    "sources",
    "nodes",
    "outputs",
    "edges",
)


def tool_result_keys(*, data: bool) -> tuple[str, ...]:
    """The top-level envelope in emission order, with or without ``data``."""
    optional = TOOL_RESULT_OPTIONAL_KEYS if data else tuple(k for k in TOOL_RESULT_OPTIONAL_KEYS if k != "data")
    return (*TOOL_RESULT_REQUIRED_KEYS, *optional)


class ValidationCodeGuidance(TypedDict):
    """The catalogue's ``(explanation, suggested_fix)`` for one closed code."""

    explanation: str
    suggested_fix: str


class ValidationGuidance(TypedDict):
    """Inline repair guidance for one failed mutation envelope.

    ``codes`` is keyed by the closed ``error_code`` so N entries sharing a
    code cost the text once. ``explain_tool`` rides only when some entry got
    no inline guidance — see ``tools.generation.build_validation_guidance``.
    """

    codes: dict[str, ValidationCodeGuidance]
    explain_tool: NotRequired[str]
```

- [ ] **Step 4: Move the TypedDicts out of `generation.py`**

Delete the two class bodies at `tools/generation.py:1687-1704` and add, next to the existing typing imports:

```python
from elspeth.web.composer.tool_result_envelope import ValidationCodeGuidance, ValidationGuidance
```

Keep the names exported (they are referenced as `generation.ValidationGuidance` by `_dispatch.py:545-573` and tests).

- [ ] **Step 5: Run the test and the guidance tests**

```bash
cd $W && PYTHONPATH=$W/src:$W/elspeth-lints/src $W/.venv/bin/python -m pytest tests/unit/web/composer/test_tool_result_envelope.py tests/unit/web/composer/test_failure_validation_guidance.py -n 0 -q
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd $W && git add src/elspeth/web/composer/tool_result_envelope.py src/elspeth/web/composer/tools/generation.py tests/unit/web/composer/test_tool_result_envelope.py && git commit -m "feat(composer): tool_result_envelope leaf — one authority for the ToolResult wire vocabulary (elspeth-e405ad7cd2)"
```

---

### Task 2: Shared teaching-gate helpers

**Files:**
- Create: `tests/unit/web/composer/_teaching_gate_support.py`
- Modify: `tests/unit/web/composer/test_planner_teaching_gate.py:100-165` (delete the moved helpers, import them)

**Interfaces:**
- Produces: `_typed_keys(payload: type, prefix: str) -> list[str]`, `_is_cast`, `_is_dict_literal`, `_literal_str`, `_call_name`, `composer_python_files() -> list[Path]`, `_display(path) -> str`, `is_quoted_leaf(key: str, text: str) -> bool`, `WEB_SRC`, `REPO_ROOT`.

- [ ] **Step 1: Create the support module by moving the helpers verbatim**

Move `_typed_keys`, `_is_cast`, `_is_dict_literal`, `_call_name`, `_literal_str`, `composer_python_files`, `_display`, `REPO_ROOT`, `WEB_SRC` from `test_planner_teaching_gate.py` into `_teaching_gate_support.py` with their bodies unchanged, and add the quoted-leaf predicate extracted from `is_taught`:

```python
def is_quoted_leaf(key: str, text: str) -> bool:
    """The key's leaf appears in house-style quoted form (``'leaf'`` or ```leaf```) in ``text``.

    Bare-word matches do not count: ordinary prose ("the consumer node") would
    otherwise teach ``consumer`` (red-team finding on bc8b9e237).
    """
    leaf = key.split(".")[-1].replace("[]", "")
    return re.search(rf"['`]{re.escape(leaf)}['`]", text) is not None
```

In the old gate, `is_taught` becomes:

```python
def is_taught(code: str, key: str, explain=generation.explain_validation_code) -> bool:
    guidance = explain(code)
    if guidance is None:
        return False
    return is_quoted_leaf(key, " ".join(guidance))
```

- [ ] **Step 2: Run the old gate unchanged in behaviour**

```bash
cd $W && PYTHONPATH=$W/src:$W/elspeth-lints/src $W/.venv/bin/python -m pytest tests/unit/web/composer/test_planner_teaching_gate.py -n 0 -q
```
Expected: PASS, same test count as `$S/gates-base.log`.

- [ ] **Step 3: Commit**

```bash
cd $W && git add tests/unit/web/composer/_teaching_gate_support.py tests/unit/web/composer/test_planner_teaching_gate.py && git commit -m "test(composer): share the teaching-gate walker helpers between gates"
```

---

### Task 3: Gate — shipped side with walker probes

**Files:**
- Create: `tests/unit/web/composer/test_tool_result_envelope_gate.py`

**Interfaces:**
- Produces: `ShippedKey(surface, tool, key, site)`, `shipped_keys() -> list[ShippedKey]`, plus the per-surface generators named below. Later tasks add `admitted_keys`, `taught_text`, `untaught_keys`, `load_fence`.

- [ ] **Step 1: Write the probe tests first (they pin the walker, not the tree)**

```python
# tests/unit/web/composer/test_tool_result_envelope_gate.py  (first cut: probes only)
"""Every key ToolResult.to_dict ships to the planner is registered, admitted, and taught or fenced.

Three sides, all derived at test time — never enumerated by hand:
  shipped  — AST of ``ToolResult.to_dict`` and the helpers it calls, plus the owned TypedDicts
  admitted — the live redaction manifest objects and the envelope registry
  taught   — the rendered system prompt and every ToolDeclaration.description
Method: docs/agents/explore-and-pin-methodology.md (elspeth-e405ad7cd2).
"""

from __future__ import annotations

import ast
import json
import re
import typing
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import NamedTuple, NotRequired, TypedDict

import pytest

from elspeth.web.composer import tool_result_envelope as env
from tests.unit.web.composer._teaching_gate_support import (
    WEB_SRC,
    _call_name,
    _display,
    _is_cast,
    _is_dict_literal,
    _literal_str,
    _typed_keys,
    composer_python_files,
    is_quoted_leaf,
)

FENCE_PATH = Path(__file__).with_name("tool_result_envelope_fence.json")
COMMON = WEB_SRC / "composer" / "tools" / "_common.py"
TOOL_BATCH = WEB_SRC / "composer" / "tool_batch.py"
SHARED = "*"  # tool column for surfaces every tool ships


class ShippedKey(NamedTuple):
    surface: str
    tool: str
    key: str
    site: str


class FenceEntry(NamedTuple):
    surface: str
    tool: str
    key: str
    reason: str


# --- walker primitives ----------------------------------------------------------------------------


def _function(tree: ast.Module, name: str, *, in_class: str | None = None) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and in_class is not None and node.name == in_class:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == name:
                    return item
        if in_class is None and isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found{' in ' + in_class if in_class else ''}")


def _dict_literal_keys(node: ast.AST, prefix: str, site: str) -> Iterator[str]:
    """Dotted keys of a dict literal, recursing through nested dict literals and list-of-dict literals.

    Refuses (AssertionError) a non-constant key, a ``**`` splat, and a ``cast(...)`` value: each
    would let a producer ship a key the walker cannot read.
    """
    assert isinstance(node, ast.Dict), f"{site}: expected a dict literal"
    for key_node, value in zip(node.keys, node.values, strict=True):
        assert key_node is not None, f"{site}: ** splat inside a shipped dict literal"
        key = _literal_str(key_node)
        assert key is not None, f"{site}: non-literal key in a shipped dict literal"
        assert not _is_cast(value), f"{site}: cast(...) hides the shape of {prefix}{key}"
        path = f"{prefix}{key}"
        yield path
        if isinstance(value, ast.Dict):
            yield from _dict_literal_keys(value, path + ".", site)
        elif isinstance(value, ast.List) and value.elts and isinstance(value.elts[0], ast.Dict):
            yield from _dict_literal_keys(value.elts[0], path + "[].", site)


def _subscript_assign_keys(fn: ast.FunctionDef, target: str, site: str) -> Iterator[tuple[str, int]]:
    """``target["key"] = ...`` statements inside ``fn`` (key, lineno)."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        tgt = node.targets[0]
        if isinstance(tgt, ast.Subscript) and isinstance(tgt.value, ast.Name) and tgt.value.id == target:
            key = _literal_str(tgt.slice)
            assert key is not None, f"{site}:{node.lineno}: non-literal key assigned into {target}"
            assert not _is_cast(node.value), f"{site}:{node.lineno}: cast(...) hides the shape of {key}"
            yield key, node.lineno


def _initial_dict_assign(fn: ast.FunctionDef, target: str, site: str) -> ast.Dict:
    for node in fn.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            tgt = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if isinstance(tgt, ast.Name) and tgt.id == target and isinstance(node.value, ast.Dict):
                return node.value
    raise AssertionError(f"{site}: {target} is not initialised from a dict literal")


# --- probes (module level: postponed annotations break function-local TypedDicts) ----------------


class _ProbeEntry(TypedDict):
    code: str
    detail: NotRequired[str]


class _ProbeEnvelope(TypedDict):
    items: list[_ProbeEntry]
    nested: _ProbeEntry


_PROBE_TO_DICT = '''
class ToolResult:
    def to_dict(self):
        result = {"success": True, "validation": {"is_valid": True, "errors": []}, "version": 1}
        if self.data is not None:
            result["data"] = self.data
        return result
'''

_PROBE_SPLAT = '''
class ToolResult:
    def to_dict(self):
        result = {"success": True, **self.extra}
        return result
'''

_PROBE_CAST = '''
class ToolResult:
    def to_dict(self):
        result = {"success": True}
        result["data"] = cast(JsonValue, self.data)
        return result
'''

_PROBE_DYNAMIC_KEY = '''
class ToolResult:
    def to_dict(self):
        result = {"success": True}
        result[self.key_name] = 1
        return result
'''


def test_walker_reads_top_level_and_nested_literal_keys_in_emission_order() -> None:
    tree = ast.parse(_PROBE_TO_DICT)
    fn = _function(tree, "to_dict", in_class="ToolResult")
    initial = list(_dict_literal_keys(_initial_dict_assign(fn, "result", "probe"), "", "probe"))
    later = [k for k, _ in _subscript_assign_keys(fn, "result", "probe")]
    assert initial == ["success", "validation", "validation.is_valid", "validation.errors", "version"]
    assert later == ["data"]


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (_PROBE_SPLAT, "** splat"),
        (_PROBE_CAST, "cast(...) hides"),
        (_PROBE_DYNAMIC_KEY, "non-literal key"),
    ],
    ids=["splat", "cast", "dynamic-key"],
)
def test_walker_refuses_shapes_it_cannot_read(source: str, message: str) -> None:
    tree = ast.parse(source)
    fn = _function(tree, "to_dict", in_class="ToolResult")
    with pytest.raises(AssertionError, match=re.escape(message)):
        list(_dict_literal_keys(_initial_dict_assign(fn, "result", "probe"), "", "probe"))
        list(_subscript_assign_keys(fn, "result", "probe"))


def test_typed_keys_recurse_through_nested_and_list_of_typed_dicts() -> None:
    assert _typed_keys(_ProbeEnvelope, "x.") == [
        "x.items", "x.items[].code", "x.items[].detail", "x.nested", "x.nested.code", "x.nested.detail",
    ]
```

- [ ] **Step 2: Run the probes**

```bash
cd $W && PYTHONPATH=$W/src:$W/elspeth-lints/src $W/.venv/bin/python -m pytest tests/unit/web/composer/test_tool_result_envelope_gate.py -n 0 -q
```
Expected: PASS (the walker primitives are self-contained). If the `cast` probe passes without raising, `_is_cast` is not being applied to the subscript value: fix the walker, not the probe.

- [ ] **Step 3: Add the shipped-side generators**

Append to the gate file:

```python
# --- shipped side ---------------------------------------------------------------------------------

from elspeth.web.composer import state as state_mod  # noqa placement: keep imports at top in the real file
from elspeth.web.composer.tools import _common as common_mod


def _envelope_sites() -> Iterator[ShippedKey]:
    tree = ast.parse(COMMON.read_text(encoding="utf-8"))
    fn = _function(tree, "to_dict", in_class="ToolResult")
    site = f"{_display(COMMON)}:{fn.lineno}"
    initial = _initial_dict_assign(fn, "result", site)
    for key in _dict_literal_keys(initial, "", site):
        surface = "validation" if key.startswith("validation.") else "envelope"
        yield ShippedKey(surface, SHARED, key, site)
    for key, lineno in _subscript_assign_keys(fn, "result", site):
        yield ShippedKey("envelope", SHARED, key, f"{_display(COMMON)}:{lineno}")


def _validation_typed_sites() -> Iterator[ShippedKey]:
    site = f"{_display(WEB_SRC / 'composer' / 'state.py')}:ValidationEntryDict"
    for list_key in ("errors", "warnings", "suggestions"):
        for key in _typed_keys(state_mod.ValidationEntryDict, f"validation.{list_key}[]."):
            yield ShippedKey("validation", SHARED, key, site)
    for key in _typed_keys(common_mod._SemanticEdgeContractPayload, "validation.semantic_contracts[]."):
        yield ShippedKey("validation", SHARED, key, f"{_display(COMMON)}:_SemanticEdgeContractPayload")
    for key in _typed_keys(common_mod._GraphRepairSuggestion, "validation.graph_repair_suggestions[]."):
        yield ShippedKey("validation", SHARED, key, f"{_display(COMMON)}:_GraphRepairSuggestion")


def _delta_sites() -> Iterator[ShippedKey]:
    tree = ast.parse(COMMON.read_text(encoding="utf-8"))
    fn = _function(tree, "_compute_validation_delta")
    site = f"{_display(COMMON)}:{fn.lineno}"
    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return) and n.value is not None]
    assert len(returns) == 1, f"{site}: expected exactly one return"
    for key in _dict_literal_keys(returns[0].value, "validation_delta.", site):
        yield ShippedKey("delta", SHARED, key, site)
        for entry_key in _typed_keys(state_mod.ValidationEntryDict, key + "[]."):
            yield ShippedKey("delta", SHARED, entry_key, site)


def _guidance_sites() -> Iterator[ShippedKey]:
    site = f"{_display(WEB_SRC / 'composer' / 'tool_result_envelope.py')}:ValidationGuidance"
    for key in _typed_keys(env.ValidationGuidance, "validation_guidance."):
        yield ShippedKey("guidance", SHARED, key, site)
    # ``codes`` is keyed by the closed error_code; its value shape is the entry TypedDict.
    for key in _typed_keys(env.ValidationCodeGuidance, "validation_guidance.codes.<code>."):
        yield ShippedKey("guidance", SHARED, key, site)


def _echo_sites() -> Iterator[ShippedKey]:
    tree = ast.parse(COMMON.read_text(encoding="utf-8"))
    fn = _function(tree, "_applied_component_echo")
    for key, lineno in _subscript_assign_keys(fn, "echo", f"{_display(COMMON)}:{fn.lineno}"):
        yield ShippedKey("echo", SHARED, f"applied_component.{key}", f"{_display(COMMON)}:{lineno}")


_FAILURE_DATA_HELPERS: tuple[tuple[Path, str, str], ...] = (
    # (file, function, dict-literal owner) — the shared helpers whose ``data`` literal every failure carries
    (COMMON, "_failure_result", "data"),
    (COMMON, "_credential_wiring_contract_failure", "data"),
    (TOOL_BATCH, "run_tool_batch", "proposal_payload"),
    (TOOL_BATCH, "run_tool_batch", "feedback_data"),
)


def _failure_data_sites() -> Iterator[ShippedKey]:
    for path, fn_name, owner in _FAILURE_DATA_HELPERS:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        fn = _function(tree, fn_name)
        site = f"{_display(path)}:{fn.lineno}"
        found = False
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == owner for t in node.targets):
                value = node.value
                # ``{**seed, "status": ...}`` — the literal keys are readable, the splat is a refusal
                # unless it resolves to a helper we own (prevalidation feed seed).
                found = True
                for key in _dict_literal_keys(value, "data.", f"{site}:{node.lineno}"):
                    yield ShippedKey("failure-data", SHARED, key, f"{_display(path)}:{node.lineno}")
            if isinstance(node, ast.Call) and _call_name(node) == "ToolResult":
                for kw in node.keywords:
                    if kw.arg == "data" and isinstance(kw.value, ast.Dict):
                        found = True
                        for key in _dict_literal_keys(kw.value, "data.", f"{site}:{node.lineno}"):
                            yield ShippedKey("failure-data", SHARED, key, f"{_display(path)}:{node.lineno}")
        assert found, f"{site}: no dict literal named {owner} or passed as data= — walker out of date"
    yield ShippedKey("failure-data", SHARED, f"data.{common_mod.COMPONENTS_WITHHELD_KEY}", f"{_display(COMMON)}:_merged_component_rejection_result")
```

Then the `tool-data` walker. The allowlist is what makes helper calls attributable; an unknown helper is a refusal, exactly like an unowned detail constructor in the previous gate:

```python
_DATA_HELPER_PAYLOADS: dict[str, type | None] = {
    # helper name -> the TypedDict / pydantic model it returns; None = list-of-str or scalar-only payload
    # Filled in during the census (Task 5). Every helper passed as ``data=`` MUST appear here.
}

_RESULT_CONSTRUCTORS = frozenset({"_discovery_result", "_mutation_result"})


def _tool_name_for(fn_name: str) -> str:
    """``_handle_list_sources`` / ``_execute_set_source`` -> ``list_sources`` / ``set_source``."""
    for prefix in ("_handle_", "_execute_", "build_"):
        if fn_name.startswith(prefix):
            return fn_name[len(prefix):]
    return fn_name


def _payload_keys(payload: type | None, prefix: str) -> list[str]:
    if payload is None:
        return []
    if typing.is_typeddict(payload):
        return _typed_keys(payload, prefix)
    fields = getattr(payload, "model_fields", None)
    assert fields is not None, f"{payload!r} is neither a TypedDict nor a pydantic model"
    return [f"{prefix}{name}" for name in fields]


def _tool_data_sites(files: Iterable[Path]) -> Iterator[ShippedKey]:
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) not in _RESULT_CONSTRUCTORS:
                continue
            fn = _enclosing_function(tree, node.lineno)
            tool = _tool_name_for(fn or "?")
            site = f"{_display(path)}:{node.lineno}"
            data = next((kw.value for kw in node.keywords if kw.arg == "data"), None)
            if data is None and _call_name(node) == "_discovery_result" and len(node.args) >= 2:
                data = node.args[1]
            if data is None or (isinstance(data, ast.Constant) and data.value is None):
                continue
            if isinstance(data, ast.Dict):
                for key in _dict_literal_keys(data, "data.", site):
                    yield ShippedKey("tool-data", tool, key, site)
            elif isinstance(data, ast.Call):
                helper = _call_name(data)
                assert helper in _DATA_HELPER_PAYLOADS, f"{site}: data built by unattributed helper {helper!r} — add it to _DATA_HELPER_PAYLOADS with its payload type"
                for key in _payload_keys(_DATA_HELPER_PAYLOADS[helper], "data."):
                    yield ShippedKey("tool-data", tool, key, site)
            elif isinstance(data, ast.Name):
                # A local carrying a typed payload: the census maps the (file, function, name) to its type.
                typed = _LOCAL_DATA_PAYLOADS.get((path.name, fn, data.id))
                assert typed is not None, f"{site}: data passed as local {data.id!r} with no payload attribution"
                for key in _payload_keys(typed, "data."):
                    yield ShippedKey("tool-data", tool, key, site)
            else:
                raise AssertionError(f"{site}: data expression {type(data).__name__} is not a literal, helper call, or attributed local")


_LOCAL_DATA_PAYLOADS: dict[tuple[str, str | None, str], type | None] = {
    # (file name, enclosing function, local name) -> payload type. Filled in during the census.
}


def shipped_keys(files: Iterable[Path] | None = None) -> list[ShippedKey]:
    paths = composer_python_files() if files is None else list(files)
    return [
        *_envelope_sites(),
        *_validation_typed_sites(),
        *_delta_sites(),
        *_guidance_sites(),
        *_echo_sites(),
        *_failure_data_sites(),
        *_tool_data_sites(paths),
    ]
```

`_enclosing_function` is the helper already in the old gate (line 144); move it to the support module in Task 2 if not already.

- [ ] **Step 4: Add the shipped-side self-tests that pin the tree, then run**

```python
def test_envelope_sites_equal_the_registry_in_emission_order() -> None:
    """to_dict's literal keys ARE the registry: a key added on one side turns this red."""
    top = [k.key for k in _envelope_sites() if k.surface == "envelope"]
    assert tuple(top) == (*env.TOOL_RESULT_REQUIRED_KEYS, *env.TOOL_RESULT_OPTIONAL_KEYS)


def test_validation_literal_keys_equal_the_registry() -> None:
    nested = [k.key.removeprefix("validation.") for k in _envelope_sites() if k.surface == "validation"]
    assert tuple(nested) == env.VALIDATION_KEYS


def test_delta_and_echo_literal_keys_equal_the_registry() -> None:
    delta = [k.key.removeprefix("validation_delta.") for k in _delta_sites() if "[]" not in k.key]
    echo = [k.key.removeprefix("applied_component.") for k in _echo_sites()]
    assert tuple(delta) == env.VALIDATION_DELTA_KEYS
    assert tuple(echo) == env.APPLIED_COMPONENT_KEYS


def test_every_result_constructor_site_is_attributed() -> None:
    """The tool-data walker must not skip a site silently: it raises on the first unattributed helper."""
    list(_tool_data_sites(composer_python_files()))
```

```bash
cd $W && PYTHONPATH=$W/src:$W/elspeth-lints/src $W/.venv/bin/python -m pytest tests/unit/web/composer/test_tool_result_envelope_gate.py -n 0 -q
```
Expected: the registry equality tests PASS; `test_every_result_constructor_site_is_attributed` FAILS naming the first unattributed helper (e.g. `facts_to_dict`). That failure is the census starting: Task 5 fills the two attribution maps.

- [ ] **Step 5: Commit the walker with the attribution test marked as the census driver**

Do not skip or xfail the failing test. Commit the file with the test failing and say so in the message; the branch is a lane branch and the next commit (Task 5) turns it green.

```bash
cd $W && git add tests/unit/web/composer/test_tool_result_envelope_gate.py && git commit -m "test(composer): tool-result envelope gate — shipped-side walker and probes (attribution map pending census)"
```

---

### Task 4: Gate — admitted and taught sides, matrix, fence tests

**Files:**
- Modify: `tests/unit/web/composer/test_tool_result_envelope_gate.py`
- Create: `tests/unit/web/composer/tool_result_envelope_fence.json`

**Interfaces:**
- Produces: `admitted_keys() -> dict[str, frozenset[str]]` (tool → admitted top-level keys), `taught_text(tool) -> str`, `is_taught(shipped) -> bool`, `untaught_keys() -> dict[tuple[str,str,str], list[str]]`, `load_fence()`, `matrix_rows() -> list[dict]`.

- [ ] **Step 1: Admitted side, derived from the live manifest**

```python
from elspeth.web.composer import redaction
from elspeth.web.composer.tools._dispatch import get_tool_definitions


def admitted_keys() -> dict[str, frozenset[str]]:
    """tool -> top-level keys the persisted audit row keeps (not sentinelled, not raised on)."""
    out: dict[str, frozenset[str]] = {}
    for name, entry in redaction.MANIFEST.items():
        if entry.response_model is not None:
            out[name] = frozenset(entry.response_model.model_fields)
        else:
            policy = entry.policy
            assert policy is not None
            out[name] = frozenset(policy.known_response_keys) | redaction._TOOL_RESULT_ENVELOPE_KEYS
    return out


def test_type_driven_response_model_fields_equal_the_registry() -> None:
    fields = tuple(redaction._ToolResultResponseModel.model_fields)
    assert fields == (*env.tool_result_keys(data=True), *env.TOOL_RESULT_POST_DISPATCH_KEYS)


def test_declarative_key_tables_equal_the_registry() -> None:
    assert redaction._tool_result_response_keys(data=True) == env.tool_result_keys(data=True)
    assert redaction._tool_result_response_keys(data=False) == env.tool_result_keys(data=False)


def test_implicit_declarative_envelope_covers_every_required_key() -> None:
    """D1: a required producer key that is not implicitly known fires the drift counter on every call."""
    assert set(env.TOOL_RESULT_REQUIRED_KEYS) <= redaction._TOOL_RESULT_ENVELOPE_KEYS


def test_closed_provider_discovery_payload_is_a_subset_of_the_registry() -> None:
    from elspeth.web.composer import pipeline_planner

    keys = set(typing.get_type_hints(pipeline_planner._ClosedProviderDiscoveryPayload))
    assert keys <= set(env.tool_result_keys(data=True))
    nested = set(typing.get_type_hints(pipeline_planner._ClosedProviderValidationEnvelope))
    assert nested <= set(env.VALIDATION_KEYS)


def test_no_shared_envelope_key_is_unadmitted_on_a_mutation_tool() -> None:
    """A shipped shared key the audit row would raise on or sentinel is a producer/manifest split."""
    shared = {k.key for k in shipped_keys() if k.surface == "envelope"}
    for tool, keys in admitted_keys().items():
        entry = redaction.MANIFEST[tool]
        if entry.response_model is not None or (entry.policy is not None and entry.policy.known_response_keys):
            missing = shared - keys - {"data"}  # data admission is per tool by design
            assert not missing, f"{tool}: shipped but unadmitted {sorted(missing)}"
```

Run: the first three FAIL until Task 9 (tables not yet derived; `affected_nodes` not implicit). That is intended; they are the admitted-side pins written first. The last two should PASS today.

- [ ] **Step 2: Taught side, derived from the rendered prompt and the declarations**

```python
from elspeth.web.composer.prompts import build_system_prompt
from elspeth.web.composer.tools.schema_contract import canonical_set_pipeline_schema


def _all_descriptions() -> dict[str, str]:
    return {d["name"]: d["description"] for d in get_tool_definitions()}


def _authoring_vocabulary() -> frozenset[str]:
    """Property names anywhere in the set_pipeline argument schema: keys the model itself authors."""
    names: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                names.update(props)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(canonical_set_pipeline_schema())
    return frozenset(names)


def taught_text(tool: str) -> str:
    skill = build_system_prompt(None)
    descriptions = _all_descriptions()
    if tool == SHARED:
        return skill + "\n" + "\n".join(descriptions.values())
    return skill + "\n" + descriptions[tool]


def is_taught(shipped: ShippedKey) -> bool:
    if is_quoted_leaf(shipped.key, taught_text(shipped.tool)):
        return True
    # Echo and state-read payloads restate authoring vocabulary the model wrote via set_pipeline.
    if shipped.surface in {"echo", "tool-data"} and "[]" in shipped.key:
        leaf = shipped.key.split(".")[-1]
        return leaf in _authoring_vocabulary()
    return False


def untaught_keys() -> dict[tuple[str, str, str], list[str]]:
    out: dict[tuple[str, str, str], list[str]] = {}
    for shipped in shipped_keys():
        if is_taught(shipped):
            continue
        out.setdefault((shipped.surface, shipped.tool, shipped.key), []).append(shipped.site)
    return out


def load_fence(path: Path = FENCE_PATH) -> list[FenceEntry]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [FenceEntry(e["surface"], e["tool"], e["key"], e["reason"]) for e in raw["fenced"]]


def matrix_rows() -> list[dict[str, object]]:
    """One row per shipped key: the census artefact for the ticket."""
    admitted = admitted_keys()
    rows = []
    for shipped in shipped_keys():
        top = shipped.key.split(".")[0]
        rows.append(
            {
                "surface": shipped.surface,
                "tool": shipped.tool,
                "key": shipped.key,
                "site": shipped.site,
                "taught": is_taught(shipped),
                "admitted_on": sorted(t for t, keys in admitted.items() if top in keys) if shipped.tool == SHARED else (top in admitted.get(shipped.tool, frozenset())),
            }
        )
    return rows
```

- [ ] **Step 3: The four fence tests and the gate**

```python
def test_every_shipped_envelope_key_is_taught_or_fenced() -> None:
    fenced = {(e.surface, e.tool, e.key) for e in load_fence()}
    unexplained = {k: v for k, v in untaught_keys().items() if k not in fenced}
    lines = [f"{s} {t} {k}  <- {', '.join(sites)}" for (s, t, k), sites in sorted(unexplained.items())]
    assert not unexplained, (
        f"{len(unexplained)} tool-result key(s) reach the planner with no prose that names them. "
        "Teach each in skills/pipeline_composer.md ('Reading a tool result') or the tool's declaration "
        "description, or fence it with a checkable reason in tool_result_envelope_fence.json:\n" + "\n".join(lines)
    )


def test_fence_entries_are_live_untaught_keys() -> None:
    untaught = untaught_keys()
    shipped = {(s.surface, s.tool, s.key) for s in shipped_keys()}
    stale = []
    for entry in load_fence():
        ident = (entry.surface, entry.tool, entry.key)
        if ident not in shipped:
            stale.append(f"{ident}: no producer ships this key any more")
        elif ident not in untaught:
            stale.append(f"{ident}: now taught — remove the fence")
    assert not stale, "stale fence entries:\n" + "\n".join(stale)


def test_fence_entries_carry_a_checkable_reason() -> None:
    placeholder = re.compile(r"^\s*(pending|todo|tbd|fixme|wip)\b", re.IGNORECASE)
    pending = [e for e in load_fence() if len(e.reason.split()) < 12 or placeholder.match(e.reason)]
    assert not pending, "fence entries await adjudication:\n" + "\n".join(f"{e.surface} {e.tool} {e.key}: {e.reason!r}" for e in pending)


def test_fence_fixture_has_no_duplicates() -> None:
    entries = load_fence()
    assert len({(e.surface, e.tool, e.key) for e in entries}) == len(entries)


def test_is_taught_requires_the_quoted_form() -> None:
    probe = ShippedKey("envelope", SHARED, "affected_nodes", "probe")
    assert not is_quoted_leaf(probe.key, "the affected nodes are listed")
    assert is_quoted_leaf(probe.key, "read `affected_nodes` first")
    assert is_quoted_leaf(probe.key, "read 'affected_nodes' first")
```

Create the fence fixture empty:

```json
{
 "_doc": "Tool-result envelope keys deliberately left untaught, each with a checkable reason. Read by tests/unit/web/composer/test_tool_result_envelope_gate.py, which fails on any 'pending' reason: every entry is an adjudicated decision (elspeth-e405ad7cd2). A fence must not outlive what it fences — the gate also fails when a fenced key becomes taught or stops shipping.",
 "fenced": []
}
```

- [ ] **Step 4: Run and record the red**

```bash
cd $W && PYTHONPATH=$W/src:$W/elspeth-lints/src $W/.venv/bin/python -m pytest tests/unit/web/composer/test_tool_result_envelope_gate.py -n 0 -q > $S/gate-first-red.log 2>&1; echo "exit=$?"
```
Expected: exit 1. The attribution test and the main gate are red; that red IS the census input.

- [ ] **Step 5: Commit**

```bash
cd $W && git add tests/unit/web/composer/test_tool_result_envelope_gate.py tests/unit/web/composer/tool_result_envelope_fence.json && git commit -m "test(composer): tool-result envelope gate — admitted and taught derivations, matrix, fence (red until census + verdicts)"
```

---

### Task 5: Census and matrix (Phase 1)

**Files:**
- Modify: `tests/unit/web/composer/test_tool_result_envelope_gate.py` (the two attribution maps only)
- Create (scratch): `$S/census.py`, `$S/matrix.md`, `$S/matrix.json`

- [ ] **Step 1: Fill the attribution maps until the walker stops refusing**

Loop: run `test_every_result_constructor_site_is_attributed`, read the named helper or local, look up its return type in the producer report (§2c and §3 of `explore-producer.md`), add the entry. Known entries from the report:

```python
_DATA_HELPER_PAYLOADS = {
    "facts_to_dict": None,                 # source_inspection.py:970 — literal-keyed dict; the walker reads its return literal in a follow-up entry
    "diff_states": None,
    "_blob_create_payload": blobs.BlobCreatePayload,
    "_serialize_full_pipeline_state": common_mod._FullPipelineStatePayload,
    "_serialize_plugin_assistance_example": None,
    "get_expression_grammar": None,
    "_sync_list_blobs": None,
    "_sync_list_ready_blob_inline_descriptors": None,
}
```

`None` entries are not free: for each, extend `_payload_keys` with a second attribution route that parses the helper's own return literal (`_function(tree, helper)` + `_dict_literal_keys`), so the key list is still derived. A helper whose return is neither a TypedDict, a pydantic model, nor a dict literal is a walker refusal and becomes a producer-fix candidate in the matrix (verdict "fix the producer": return an owned TypedDict).

- [ ] **Step 2: Emit the matrix**

```python
# $S/census.py
import json, os, sys
sys.path.insert(0, os.environ["W"])   # export W from the shell that resolved the worktree root
from tests.unit.web.composer import test_tool_result_envelope_gate as gate

rows = gate.matrix_rows()
json.dump(rows, open(sys.argv[1] + "/matrix.json", "w"), indent=1)
by_surface = {}
for r in rows:
    by_surface.setdefault(r["surface"], []).append(r)
with open(sys.argv[1] + "/matrix.md", "w") as out:
    out.write(f"# Census @ {sys.argv[2]}: {len(rows)} shipped rows, {len({(r['surface'], r['tool'], r['key']) for r in rows})} distinct keys\n\n")
    for surface, items in by_surface.items():
        untaught = [r for r in items if not r["taught"]]
        out.write(f"## {surface}: {len(items)} rows, {len(untaught)} untaught\n\n| tool | key | taught | admitted | site |\n|---|---|---|---|---|\n")
        for r in items:
            out.write(f"| {r['tool']} | `{r['key']}` | {'yes' if r['taught'] else 'NO'} | {r['admitted_on']} | {r['site']} |\n")
        out.write("\n")
```

```bash
cd $W && PYTHONPATH=$W/src:$W/elspeth-lints/src $W/.venv/bin/python $S/census.py $S $(git rev-parse --short HEAD) && head -5 $S/matrix.md
```

- [ ] **Step 3: Commit the attribution maps and post the census numbers**

```bash
cd $W && git add tests/unit/web/composer/test_tool_result_envelope_gate.py && git commit -m "test(composer): envelope gate — attribute every data= helper and local to its payload type (census complete)"
```
Ticket comment: rows, distinct keys, untaught per surface, walker refusals that became producer-fix candidates. These numbers come from `matrix.json`, nothing else.

---

### Task 6: Verdicts and ratification (Phases 2 and 3) — HARD STOP

**Files:**
- Create (scratch): `$S/verdicts.md`

- [ ] **Step 1: Write one verdict per untaught row**

Table columns: surface, tool, key, verdict (`teach` / `fence` / `fix-producer` / `retire`), one-line reason, and for `teach` the draft sentence. Group: fences and producer fixes first (the decisions), then teach rows (confirmations). Known candidates going in:

| row | verdict | reason |
|---|---|---|
| `envelope success`, `version`, `affected_nodes` | teach | framing every result carries; one sentence each in the new skill section |
| `validation.warnings`, `suggestions`, `semantic_contracts[].*`, `graph_repair_suggestions[].*` | teach | the repair inputs the model reads on every failure |
| `validation_delta.*` | teach | the container is taught; its four sub-keys are not |
| `validation_guidance.*` | teach | attached to every failed mutation; taught nowhere today |
| `applied_component.*` (five component keys) | teach | container taught; component keys not |
| `data.error`, `data.error_code` | teach | the failure shape on ~260 sites |
| `data.credential_fields`, `components[]`, `repair.*` | teach | closed repair instructions the model must follow |
| `data.components_withheld` | teach or fence | decide by whether the model can act on truncation |
| `data.status` / `applied` / `proposal_id` … (proposal + prevalidation) | teach | custody outcomes the model must relay to the user |
| `runtime_preflight` | teach | preview-only; taught with `preview_pipeline`'s description |
| `diff_pipeline data.error` on success | fix-producer (D5) | contradiction between `success` and `error` |
| `tool-data` rows | per D3 | per-tool description teaches, or sibling ticket with fences naming it |

- [ ] **Step 2: Walk John through it**

Deliver `$S/verdicts.md` as the walkthrough plus D1–D5. Record John's per-row answers on the ticket (comment from `claude-fable` quoting the ruling with the date). Do not proceed past this step on silence.

---

### Task 7: Teach (Phase 2 execution)

**Files:**
- Modify: `src/elspeth/web/composer/skills/pipeline_composer.md` (new section after "Tool Inventory", line ~240)
- Modify: tool declaration descriptions per verdict
- Modify: `tests/unit/web/composer/tool_result_envelope_fence.json` per verdict
- Test: the gate, `tests/unit/web/composer/test_prompts.py`, `test_capability_skill_identity.py`, `test_tool_declarations.py`

- [ ] **Step 1: Add the skill section (draft; the ratified verdicts decide the final key list)**

```markdown
## Reading a tool result

Every tool returns one JSON object with the same framing. `success` is the
outcome; `version` is the state version after the call; `affected_nodes` lists
the component ids the call touched. `validation` is the whole-document check
after the call: `is_valid`, and `errors` / `warnings` / `suggestions` entries,
each with `component`, `message`, `severity`, and a closed `error_code` (plus
`contract`, `row_union_schema`, or `coalesce_union_type` facts when the
code carries them). `semantic_contracts` lists each edge's producer/consumer
field outcome with its `requirement_code`; `graph_repair_suggestions` gives a
ready `tool_sequence` for a duplicate-consumer repair — apply it rather than
re-deriving it.

A failed mutation carries `data.error` and `data.error_code`, and
`validation_guidance`: `codes` maps each `error_code` to an `explanation` and a
`suggested_fix`; when `explain_tool` is present, call `explain_validation_error`
with the exact code. `plugin_schemas` (when present) is the option schema for
each plugin the failure named. A credential failure carries
`data.credential_fields`, `data.components`, and `data.repair` with an
`inline_form` and a `post_hoc_form`; follow one of them exactly.

A successful incremental mutation carries `applied_component` — `source`,
`sources`, `nodes`, `outputs`, `edges` as stored — and `validation_delta`:
`new_errors`, `resolved_errors`, `new_warnings`, `resolved_warnings`. Read the
delta to decide the next repair; never re-read state to confirm the echo.
`post_call_hints` are plugin-authored next steps; `runtime_preflight` appears
only on `preview_pipeline`.
```

Keep the existing sentence at line 230-231 verbatim (pinned by `test_prompts.py:819`).

- [ ] **Step 2: Per-tool descriptions** — for each `tool-data` teach verdict, add the quoted key to that tool's `ToolDeclaration.description`. Example for `diff_pipeline` (`generation.py:3966`): "Returns `from_version`, `to_version`, `nodes`/`edges`/`outputs` with `added`, `removed`, `modified`, plus `warnings_introduced` and `warnings_resolved`."

- [ ] **Step 3: Fence per verdict** — each fenced row gets `{"surface","tool","key","reason"}` with a reason of at least twelve words that a reviewer can check against the code.

- [ ] **Step 4: Run the gate and prompt pins**

```bash
cd $W && PYTHONPATH=$W/src:$W/elspeth-lints/src $W/.venv/bin/python -m pytest tests/unit/web/composer/test_tool_result_envelope_gate.py tests/unit/web/composer/test_prompts.py tests/unit/web/composer/test_capability_skill_identity.py tests/unit/web/composer/test_tool_declarations.py tests/unit/web/composer/test_skills_loader.py -n 0 -q > $S/teach.log 2>&1; echo "exit=$?"; tail -3 $S/teach.log
```
Expected: exit 0 except the three admitted-side pins (Task 9).

- [ ] **Step 5: Commit**

```bash
cd $W && git add src/elspeth/web/composer/skills/pipeline_composer.md src/elspeth/web/composer/tools/*.py tests/unit/web/composer/tool_result_envelope_fence.json && git commit -m "feat(composer): teach the tool-result envelope — 'Reading a tool result' skill section, tool-data descriptions, fence (elspeth-e405ad7cd2)"
```

---

### Task 8: Producer fixes (per verdict)

**Files:**
- Modify: `src/elspeth/web/composer/tools/generation.py:3938-3948` (D5), `:1684` (`explain_tool` names its container)
- Test: `tests/unit/web/composer/test_generation_tools.py` (or the file holding `diff_pipeline` tests; find it with `grep -rl "diff_pipeline" tests/unit/web/composer`)

- [ ] **Step 1: Failing test for D5**

```python
def test_diff_pipeline_without_baseline_is_a_failure_not_a_success_with_an_error_key() -> None:
    result = _execute_diff_pipeline(state, context=_context_without_baseline())
    assert result.success is False
    assert result.data["error_code"] == "diff_baseline_unavailable"
    assert "error" in result.data
```

- [ ] **Step 2: Implement** — replace the `_discovery_result(state, {error, current_version})` at `generation.py:3944` with `_failure_result(state, "No baseline available. Load or create a session first.", error_code="diff_baseline_unavailable", with_state_validation=True)` and add the code to `_VALIDATION_ERROR_PATTERNS` with an explanation and a fix (the planner teaching gate will demand it).

- [ ] **Step 3: `explain_tool` sentence** — `EXPLAIN_VALIDATION_ERROR_GUIDANCE = "To expand any code under 'validation_guidance.codes', call explain_validation_error with the exact code string."` and update `test_failure_validation_guidance.py`'s pin.

- [ ] **Step 4: Run both teaching gates and the guidance tests; commit**

```bash
cd $W && PYTHONPATH=$W/src:$W/elspeth-lints/src $W/.venv/bin/python -m pytest tests/unit/web/composer/test_planner_teaching_gate.py tests/unit/web/composer/test_tool_result_envelope_gate.py tests/unit/web/composer/test_failure_validation_guidance.py -n 0 -q; git add -A src/elspeth/web/composer/tools/generation.py tests/unit/web/composer && git commit -m "fix(composer): diff_pipeline without a baseline fails closed; explain_tool names its container (elspeth-e405ad7cd2 D5)"
```

---

### Task 9: Structural close — ToolResult field types and redaction derives from the registry (Phase 5)

**Files:**
- Modify: `src/elspeth/web/composer/tools/_common.py:876-956` (fields, admission), `:3715` (`AppliedComponentEcho` return type)
- Modify: `src/elspeth/web/composer/redaction.py:92`, `:3561-3582`
- Modify: `tests/unit/web/composer/test_redact_tool_call_response.py:1210-1246`, `tests/unit/web/composer/test_declarative_manifest_runtime_smoke.py:158-163`
- Test: `tests/unit/web/composer/test_tool_result_admission.py` (new)

- [ ] **Step 1: Failing admission tests**

```python
# tests/unit/web/composer/test_tool_result_admission.py
"""ToolResult admits only closed payload shapes (ADR-032: validate by trust domain)."""

from types import MappingProxyType

import pytest
from pydantic import BaseModel

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.tools._common import ToolResult


class _Model(BaseModel):
    x: int = 1


class _DictLookalike(dict): ...


@pytest.mark.parametrize(
    "data",
    [{"a": 1}, MappingProxyType({"a": 1}), [{"a": 1}], ({"a": 1},), _Model(), None],
    ids=["dict", "proxy", "list", "tuple", "model", "none"],
)
def test_admits_every_closed_data_shape(data, minimal_state, empty_validation) -> None:
    ToolResult(success=True, updated_state=minimal_state, validation=empty_validation, affected_nodes=(), data=data)


@pytest.mark.parametrize("data", [_DictLookalike(a=1), "text", 1, 1.0, {1, 2}, object()], ids=["dict-subclass", "str", "int", "float", "set", "object"])
def test_refuses_an_open_data_shape(data, minimal_state, empty_validation) -> None:
    with pytest.raises(AuditIntegrityError, match="ToolResult.data"):
        ToolResult(success=True, updated_state=minimal_state, validation=empty_validation, affected_nodes=(), data=data)


def test_refuses_validation_guidance_without_codes(minimal_state, empty_validation) -> None:
    with pytest.raises(AuditIntegrityError, match="validation_guidance"):
        ToolResult(success=False, updated_state=minimal_state, validation=empty_validation, affected_nodes=(), validation_guidance={"explain_tool": "x"})


def test_refuses_applied_component_with_a_key_outside_the_registry(minimal_state, empty_validation) -> None:
    with pytest.raises(AuditIntegrityError, match="applied_component"):
        ToolResult(success=True, updated_state=minimal_state, validation=empty_validation, affected_nodes=(), applied_component={"gates": []})
```

Use the fixtures the existing `ToolResult` tests already use for a minimal `CompositionState` and empty `ValidationSummary` (find them with `grep -rn "ToolResult(" tests/unit/web/composer | head`).

- [ ] **Step 2: Types and admission in `_common.py`**

```python
from collections.abc import Mapping, Sequence
from types import MappingProxyType

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.tool_result_envelope import APPLIED_COMPONENT_KEYS, ValidationGuidance

ToolResultData = Mapping[str, object] | Sequence[object] | BaseModel
_DATA_CONTAINER_TYPES: Final[tuple[type, ...]] = (dict, MappingProxyType, list, tuple)


class AppliedComponentEcho(TypedDict, total=False):
    """The post-change components a successful incremental mutation echoes (see _applied_component_echo)."""

    source: dict[str, JsonValue]
    sources: dict[str, dict[str, JsonValue]]
    nodes: list[_SetPipelineNodePayload]
    outputs: list[dict[str, JsonValue]]
    edges: list[dict[str, JsonValue]]


def _require_tool_result_data(value: object) -> None:
    if value is None or isinstance(value, BaseModel) or type(value) in _DATA_CONTAINER_TYPES:
        return
    raise AuditIntegrityError(f"ToolResult.data is not a closed payload shape: {type(value).__name__}")


def _require_validation_guidance(value: object) -> None:
    if value is None:
        return
    if type(value) not in (dict, MappingProxyType) or type(value.get("codes")) not in (dict, MappingProxyType):
        raise AuditIntegrityError("ToolResult.validation_guidance must carry a 'codes' mapping")


def _require_applied_component(value: object) -> None:
    if value is None:
        return
    if type(value) not in (dict, MappingProxyType) or not set(value) <= set(APPLIED_COMPONENT_KEYS):
        raise AuditIntegrityError(f"ToolResult.applied_component keys outside the registry: {sorted(set(value) - set(APPLIED_COMPONENT_KEYS))}")
```

Field block:

```python
    data: ToolResultData | None = None
    prior_validation: ValidationSummary | None = None
    runtime_preflight: ValidationResult | None = None
    post_call_hints: tuple[str, ...] = ()
    plugin_schemas: Mapping[str, Mapping[str, JsonValue]] | None = None
    validation_guidance: ValidationGuidance | None = None
    applied_component: AppliedComponentEcho | None = None
```

`__post_init__` calls the three admission functions before `freeze_fields`. `_applied_component_echo` returns `AppliedComponentEcho | None` and its local `echo: AppliedComponentEcho = {}`. `build_plugin_schemas_for_failure` returns `Mapping[str, Mapping[str, JsonValue]] | None`.

- [ ] **Step 3: mypy the package and fix every site it names** — the `data=` producers passing a non-conforming type are fixed at the site (a `dict[str, Any]` local becomes a TypedDict or `dict[str, JsonValue]`). Strip every `cast(JsonValue, ...)` or `cast(Any, ...)` around these fields that the new types make unnecessary; keep only those mypy still requires and list them in the ticket.

```bash
cd $W && PYTHONPATH=$W/src:$W/elspeth-lints/src $W/.venv/bin/python -m mypy src/elspeth/web/composer > $S/mypy-after.log 2>&1; echo "exit=$?"; diff <(tail -1 $S/mypy-base.log) <(tail -1 $S/mypy-after.log)
```

- [ ] **Step 4: Redaction derives from the registry (D1 included)**

```python
# redaction.py
from elspeth.web.composer.tool_result_envelope import TOOL_RESULT_OPTIONAL_KEYS, TOOL_RESULT_REQUIRED_KEYS, tool_result_keys

_TOOL_RESULT_ENVELOPE_KEYS: frozenset[str] = frozenset(TOOL_RESULT_REQUIRED_KEYS)   # D1: affected_nodes joins success/validation/version

_TOOL_RESULT_REQUIRED_RESPONSE_KEYS: tuple[str, ...] = TOOL_RESULT_REQUIRED_KEYS
_TOOL_RESULT_OPTIONAL_RESPONSE_KEYS: tuple[str, ...] = tuple(k for k in TOOL_RESULT_OPTIONAL_KEYS if k != "data")


def _tool_result_response_keys(*, data: bool) -> tuple[str, ...]:
    """Return the shared top-level ``ToolResult.to_dict`` response envelope — derived from the registry."""
    return tool_result_keys(data=data)
```

Update the docstring at `redaction.py:86-91` to say `affected_nodes` is implicit and why (node ids are projected to text sentinels by `_project_untrusted_response_structure`, same as the type-driven path). Update `test_tool_result_envelope_keys_are_implicitly_known_for_declarative_entries` to assert `affected_nodes` is admitted and projected, and the runtime-smoke docstring at `test_declarative_manifest_runtime_smoke.py:158-163`. Add to `test_redact_tool_call_response.py`:

```python
def test_declarative_discovery_row_no_longer_fires_the_unknown_key_counter_on_affected_nodes() -> None:
    telemetry = NoopRedactionTelemetry()
    redact_tool_call_response("list_sources", {"success": True, "validation": _empty_validation(), "affected_nodes": [], "version": 3}, telemetry)
    assert telemetry.unknown_response_key_calls == []
```

- [ ] **Step 5: Snapshot and the property test's dead assertion**

```bash
cd $W && PYTHONPATH=$W/src:$W/elspeth-lints/src $W/.venv/bin/python -m pytest tests/unit/web/composer/test_adequacy_guard.py -n 0 -q
```
If `test_redaction_policy_snapshot_matches_live_manifest` fails, regenerate with `$W/.venv/bin/python scripts/cicd/bootstrap_redaction_snapshot.py --write` and review the diff (it should be empty: the policies' `known_response_keys` tuples are byte-identical). Replace the dead `if "stray_provider_field" in payload` branch in `tests/property/web/composer/test_compose_loop_invariants.py:309-310` with `assert payload.get("_unknown_response") == REDACTED_UNKNOWN_RESPONSE_KEY` for the stray case, so the invariant actually fires.

- [ ] **Step 6: Run the gate, redaction, admission and property suites; commit**

```bash
cd $W && PYTHONPATH=$W/src:$W/elspeth-lints/src $W/.venv/bin/python -m pytest tests/unit/web/composer/test_tool_result_envelope_gate.py tests/unit/web/composer/test_tool_result_admission.py tests/unit/web/composer/test_redact_tool_call_response.py tests/unit/web/composer/test_declarative_manifest_runtime_smoke.py tests/unit/web/composer/test_adequacy_guard.py tests/unit/web/sessions/test_tool_invocation_redaction.py tests/property/web/composer/test_compose_loop_invariants.py -q > $S/close.log 2>&1; echo "exit=$?"; tail -3 $S/close.log
git add -A src/elspeth/web/composer tests/unit/web/composer tests/property/web/composer && git commit -m "refactor(composer): close ToolResult's four loose fields with owned types + nominal admission; redaction derives the envelope from the registry (elspeth-e405ad7cd2, D1/D2)"
```

---

### Task 10: Operator reference regenerated from the registry

**Files:**
- Modify: `docs/reference/composer-tools.md:530-558`
- Test: `tests/unit/web/composer/test_tool_result_envelope_gate.py` (one more pin)

- [ ] **Step 1: Pin** — `test_operator_reference_names_every_registry_key`: read the markdown between the "Tool Result Format" heading and the next `## `, assert every key in `tool_result_keys(data=True)` and `VALIDATION_KEYS` appears in backticks.
- [ ] **Step 2: Rewrite the section** to list the registry keys with one line each, drop "null for mutations", and add the seven optional keys.
- [ ] **Step 3: Commit** `docs(composer): tool result format reference derived from the envelope registry`.

---

### Task 11: Mutation ledger (Phase 7)

**Files:**
- Create (scratch): `$S/mutations.md`

- [ ] **Step 1: For each guard, apply, run, restore by cp**

| # | mutation | expected red |
|---|---|---|
| M1 | remove `"affected_nodes"` from `TOOL_RESULT_REQUIRED_KEYS` | `test_envelope_sites_equal_the_registry_in_emission_order`, `test_type_driven_response_model_fields_equal_the_registry` |
| M2 | add `result["extra"] = 1` to `to_dict` | registry equality + `test_no_shared_envelope_key_is_unadmitted_on_a_mutation_tool` |
| M3 | delete the `validation_guidance` paragraph from the skill | `test_every_shipped_envelope_key_is_taught_or_fenced` |
| M4 | change `applied_component` teaching to a bare word | same |
| M5 | add a fence entry for a taught key | `test_fence_entries_are_live_untaught_keys` |
| M6 | drop `affected_nodes` from `_TOOL_RESULT_ENVELOPE_KEYS` | `test_implicit_declarative_envelope_covers_every_required_key`, the new counter test |
| M7 | make `_require_tool_result_data` accept `dict` subclasses | `test_refuses_an_open_data_shape[dict-subclass]` |
| M8 | add `gates: NotRequired[list]` to `AppliedComponentEcho` but not the registry | `test_delta_and_echo_literal_keys_equal_the_registry` stays green (echo walker reads code, not the TypedDict) — record this honestly as a gap and close it by pinning `get_type_hints(AppliedComponentEcho).keys() == set(APPLIED_COMPONENT_KEYS)` |
| M9 | add a `data=some_helper()` site with an unknown helper | `test_every_result_constructor_site_is_attributed` |
| M10 | wrap a shipped literal value in `cast(JsonValue, ...)` | walker refusal |

Procedure for each:

```bash
cp $W/<file> $S/mut/<file>.orig && <apply edit with the Edit tool> && cd $W && PYTHONPATH=$W/src:$W/elspeth-lints/src $W/.venv/bin/python -m pytest tests/unit/web/composer/test_tool_result_envelope_gate.py tests/unit/web/composer/test_tool_result_admission.py -n 0 -q > $S/mut/M<n>.log 2>&1; echo "M<n> exit=$?"; cp $S/mut/<file>.orig $W/<file>
```
Verify with `git diff --stat` that the tree is clean after every restore. A mutation whose edit did not apply (string not found) is recorded as NOT RUN, never as passed. Any survivor goes to the review as a finding.

- [ ] **Step 2: Ticket comment** with the ledger.

---

### Task 12: Review to zero (Phase 6)

- [ ] **Step 1: Spawn three reviewers on the branch tip**, each given the ticket, `$S/matrix.md`, `$S/verdicts.md`, `$S/mutations.md`, the diff range `d14dd221f..HEAD`, and told to write their report to `$S/review/<seat>/report.md` (agents that cannot write hand the text back; save it yourself):
  - `red-team` (adversarial): disprove the gate — a way to ship a key it does not see; a fix whose test survives reversion; the admission functions bypassed via `dataclasses.replace`.
  - `yzmir-llm-specialist:llm-diagnostician` (LLM seat): is the "Reading a tool result" section actionable from the wire alone; does any sentence contradict the existing echo rule at lines 44-49; does teaching `graph_repair_suggestions.tool_sequence` risk the model echoing raw arguments.
  - `yzmir-systems-thinking:leverage-analyst` (systems seat): second-order effects of the registry (what else should derive from it: MCP server `composer_mcp/server.py:387-401`, `discovery_cache`, `_ClosedProviderDiscoveryPayload`), and the D4 question.
- [ ] **Step 2: Fix rounds** — every finding goes back to its originator for a per-finding sign-off; repeat until a round returns nothing. Save each round's sign-off under `$S/review/<seat>/signoff-<n>.md`.
- [ ] **Step 3: Ticket comment** naming the commit all three signed off on.

---

### Task 13: Evidence (Phase 8)

- [ ] **Step 1: Full suite in the worktree, detached**

```bash
cd $W && setsid nohup bash -c "PYTHONPATH=$W/src:$W/elspeth-lints/src $W/.venv/bin/python -m pytest tests/ > $S/fullsuite-$(git rev-parse --short HEAD).log 2>&1; echo \$? > $S/fullsuite.done" > /dev/null 2>&1 &
```
Poll `$S/fullsuite.done` with Monitor; read the exit code from the file, then the summary line. Re-run any `e2e/recovery`, `integration/pipeline`, or `unit/engine/orchestrator` red with `-n 0` before attributing it.

- [ ] **Step 2: Lint corpus delta** — repeat Task 0 step 2 against `$W/src/elspeth` into `$S/lint-after.log`; report base count, after count, and the per-file diff for the touched files.
- [ ] **Step 3: `scripts/check_contracts.py`** and the mypy delta from Task 9.
- [ ] **Step 4: ruff** on the touched files.
- [ ] **Step 5: Ticket comment** with the four numbers and the commit they were measured on.

---

### Task 14: Land and deploy (Phase 9)

- [ ] **Step 1: Freeze the tree** — `git rev-parse HEAD @{u}` on `release/0.8.0` in the main checkout before and after; abort if HEAD moved during the merge.
- [ ] **Step 2: Merge** `git merge --no-ff strany/tool-result-envelope` onto `release/0.8.0`, push, then run the merged-tree suite the same way as Task 13 in a fresh worktree of the merge commit.
- [ ] **Step 3: Deploy** `sudo -n /usr/bin/systemctl restart elspeth-web.service`, then `curl --unix-socket /run/elspeth/uvicorn.sock http://localhost/api/system/status` and confirm the session-schema epoch.

---

### Task 15: Live trial (Phase 10)

- [ ] **Step 1: Before-measure on the deployed base** (do this BEFORE Task 14's restart): run `complaint_triage` from `evals/composer-standard-battery/battery.md` through the API and count, in `GET /api/sessions/{id}/messages?include_tool_rows=true`, tool rows whose content contains `"_unknown_response"`, per tool name. Save as `$S/live/before.json`.
- [ ] **Step 2: After-measure** on the deployed merge: same three battery scenarios plus one seam scenario — a `set_pipeline` with two option-shape errors so the envelope carries `plugin_schemas` and `validation_guidance` — and count: unknown-response rows per tool (expect zero for `affected_nodes`), `repair_turns_used`, tool calls per transition, and whether the model's next call after a failure uses a `suggested_fix` verbatim (read the assistant turn).
- [ ] **Step 3: Ticket comment** with per-scenario pass/fail and the counts before and after.

---

### Task 16: Close (Phase 11)

- [ ] Close elspeth-e405ad7cd2 with `close_commit=release/0.8.0@<merge sha>` and the Task 13/15 numbers in the reason.
- [ ] If D3 produced a sibling ticket, its census and fence entries are already attached; link it from the close reason.
- [ ] Update `docs/agents/explore-and-pin-methodology.md` §2 table (state of this seam) and §17 (effort of the second run), and memory.

---

## Self-review

**Spec coverage** (methodology §16 checklist → task): census by AST (3, 5), matrix with verdicts (5, 6), ratification (6), gate with probes + gated fence (3, 4), structural close (9), three sign-offs (12), mutation ledger (11), suite + lint + mypy (13), merge/deploy (14), live trial (15), close (16). Operator reference (10) is extra and cheap.

**Placeholders:** the two attribution maps in Task 3 start empty by design and are filled by Task 5's loop; that is the census, not a TODO. Task 8 depends on verdicts and names its concrete candidates. Task 12's reviewer charters are concrete.

**Type consistency:** `tool_result_keys(data=...)` (Task 1) is what Task 4 and Task 9 call; `ShippedKey(surface, tool, key, site)` is used identically in Tasks 3, 4, 5, 11; `AppliedComponentEcho` and `APPLIED_COMPONENT_KEYS` are defined in Task 9 / Task 1 and pinned in Task 11 M8.
