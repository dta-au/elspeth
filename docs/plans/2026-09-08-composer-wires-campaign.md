# Composer Wires Campaign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Every seam below is one run of the `explore-and-pin` skill (`.claude/skills/explore-and-pin/SKILL.md`); this plan says which seam, in what order, against which measured baseline.

**Goal:** Every knob the planner can set on any of the 42 composer tools is provably connected to every wire that carries it, with a whole-tree gate per wire deriving both sides from source, so the composer has the best chance of succeeding on the first turn.

**Architecture:** The campaign walks the six wires (SHIPPED, MODEL, READ, ADMITTED, TAUGHT, FRONTEND) one wire at a time across all 42 tools, the way the three landed runs went, rather than one tool at a time. Two wires already have whole-tree gates (SHIPPED against ADMITTED for arguments; the whole response envelope). Each remaining wire gets a census by AST, a ratified matrix, a derived gate that extends the existing authority (`tools/schema_contract.py`, `test_tool_argument_wire_parity.py`) rather than a parallel one, and a structural close where a type can carry the invariant. The campaign ends with a per-tool scorecard derived from the gates and a live battery trial.

**Tech Stack:** Python 3.12 `ast`, pydantic v2 `model_fields`, the live registry `tools/_dispatch.get_tool_definitions()` and `redaction.MANIFEST`, pytest whole-tree gates via `tests/helpers/tree_gate.iter_gate_sources`, vitest for the TypeScript wire, `scripts/check_contracts.py` for the soft-mapping census, `elspeth-lints check --rules all` in shape-only verify mode.

**Spec:** `.claude/skills/explore-and-pin/SKILL.md` (the technique) and epic `elspeth-54bd0b84cd` (the six-wire criterion and its founding instance). Long-form record: `docs/agents/explore-and-pin-methodology.md`.

## Global Constraints

- **No commits until the signing campaign lands** (John, 2026-09-08). Every "Commit" step below is suspended: leave the work uncommitted in the worktree, record the file set and the measurement on the ticket instead. The ban lifts only when John says so.
- **Not a lane.** No metacontroller file-set declarations, no seats-as-agents, no merge protocol. Ordinary commit hygiene once commits are allowed. Reviewer charters (adversarial / LLM / systems) are subagents or separate passes per the skill §8.
- **Workspace:** `.claude/worktrees/soft-type-burndown`, branch `composer-wires`, == `release/0.8.0` @ `3ab58f336` at campaign start. Every command: `cd "$(git rev-parse --show-toplevel)/.claude/worktrees/soft-type-burndown" && PYTHONPATH=$PWD/src:$PWD/elspeth-lints/src ...` with `-o "pythonpath=$PWD/src $PWD/elspeth-lints/src"` on pytest; verify `elspeth.__file__` resolves into the worktree before trusting a number.
- **Census by AST or live import, never grep.** A grep number is orientation and is labelled so. Counts on tickets carry the commit they were measured on and say which "distinct" they mean.
- **Gates derive, never enumerate.** The only admitted hand-maintained input is `PROJECTED_TOOLS` (a TypeScript switch Python cannot read), and Seam 4 converts it to a derivation.
- **Extend the existing authority.** SHIPPED-side gates extend `src/elspeth/web/composer/tools/schema_contract.py` and `tests/unit/web/composer/test_tool_argument_wire_parity.py`; do not add a second module that checks one pair of wires.
- **Evidence bar** (skill §10): suite failing SET diffed against the baseline set below, lint corpus diffed against the baseline count with positions masked, `check_contracts` census delta, mypy on touched packages, mutation ledger with reconciled selections, live trial counts.
- **Composer invariants** (AGENTS.md): nothing here authors pipeline structure server-side; nothing is tutorial-special. Enabling a suppressed widget on the rootless entry path (Seam 4, `134b9a9a65`) gets per-transition provider-call scrutiny.
- **Sibling suites** run on this box from Codex sandboxes; cap pytest at `-n 6` while any are running (`pgrep -af pytest` first).

---

## Baseline (measured 2026-09-08 on `composer-wires` @ `3ab58f336`)

| Instrument | Value |
|---|---|
| Live registry (`get_tool_definitions()`) | 42 tools, 104 knobs (sum of `parameters.properties`) |
| Zero-knob tools | 10: `list_blobs`, `list_composer_blobs`, `list_sources`, `get_expression_grammar`, `get_audit_info`, `preview_pipeline`, `diff_pipeline`, `list_transforms`, `list_sinks`, `list_secret_refs` |
| Redaction `MANIFEST` | 42 entries, all `ToolRedaction(argument_model, policy, response_model)`; registry and manifest name sets are equal |
| Type-driven entries (`argument_model` and `response_model` set, `policy` None) | 12: `create_blob`, `get_blob_content`, `patch_node_options`, `patch_output_options`, `patch_source_options`, `request_interpretation_review`, `set_pipeline`, `set_source`, `set_source_from_blob`, `set_source_from_blobs`, `splice_transform`, `update_blob` |
| Declarative entries with `known_argument_keys` | 15: `clear_source`, `delete_blob`, `get_blob_metadata`, `inspect_source`, `remove_edge`, `remove_node`, `remove_output`, `request_advisor_hint`, `set_metadata`, `set_output`, `upsert_edge`, `upsert_node`, `validate_secret_ref`, `wire_blob_inline_ref`, `wire_secret_ref` |
| Entries with neither model nor allowlist | 15: the 10 zero-knob tools plus **`explain_validation_error` (1 knob), `get_pipeline_state` (1), `get_plugin_schema` (2), `get_plugin_assistance` (3), `list_models` (2)** — 9 knobs with no ADMITTED wire at all; Seam 1 census must say what happens to them |
| Handler-side models | orientation grep: 6 handlers call `_validate_mutation_arguments(<Model>, args, ...)`; 32 `_execute_*` handlers in total. Seam 1 replaces this with an AST census |
| Existing whole-tree gates | `test_tool_result_envelope_gate.py` (response envelope, all keys), `test_tool_argument_wire_parity.py` (SHIPPED vs ADMITTED, argument knobs), `test_planner_teaching_gate.py` (repair-feedback facts), `schema_contract.py` (SHIPPED vs MODEL, `upsert_node` and `set_pipeline` only), `test_proposal_diff_redaction_fixture.py` (frontend fixture, values and key order) |
| Frontend projection | `ProposalDiff.tsx` switch: 13 arms; `PROJECTED_TOOLS` frozenset: the same 13 names, hand-maintained |
| Soft-mapping census (`check_contracts.py`) | exit 0; 2693 soft occurrences across 382 files, 63 boundary-parsed |
| Trust-tier lint corpus (shape-only verify mode) | exit 1 (deliberate fail-closed state), 1977 finding rows |
| Full suite `pytest tests/ -n 6` | measured 2026-09-08 on 3ab58f336 in the `soft-type-burndown` worktree (PYTHONPATH exported + `-o pythonpath`): exit 1, **1 failed / 48792 passed / 79 skipped / 6 xfailed** in 25m12s. The one red is INHERITED from the tip commit itself: `tests/unit/core/landscape/test_database_clock_authority.py::test_clock_authority_definition_inventory_is_closed_and_stable` (3ab58f336 added three clock-boundary functions in `src/elspeth/engine/orchestrator/abandon.py` — `inspect_leaderless_run`, `abandon_leaderless_run`, `_resume_verdict` — without re-pinning the inventory; additive pin, re-pin by UNION). Not this campaign's to fix; every gate in this plan is scored as NEW = failing set minus this one id. The release memory's "8 failed" was measured on ad5421518 (origin), 31 unpushed commits earlier; 56e7ccaca re-pinned three of those and the rest cleared along the way |

Open epic children slotted by wire (all `elspeth-` ids): Seam 1 — none yet (the founding instance `2cdf71397b` is closed). Seam 3 — `657f603fcd` (cross-tool quotation admits a leaf). Seam 4 — `7cda5664b0`, `d6147d73ed`, `134b9a9a65`, `f491fca94e`. Seam 5 — `7980efe197`, `8fe09316ab`, `72ce6749ac`, `e12dce8ed6`, `9fcf465c41`, `5e81b50f2e`, `d83095ee87`, `c00e6d9795`, `6aa477c78e`, `9e76d9436b`. Outside this campaign (design decisions, not wires): `10f818998e` deep_thaw, `6089bfa8fa` byte ledger, `91133e850b` / `e25f8f7530` TS mirrors, `1eca86caa9` operator tier-model worklist, `8b0b6e5bb9`, `f4c71c3e8e`, `919cd29876`, the four Wave-1 residues (`4ddaee2202`, `c8f8318203`, `caae752e11`, `6bcc0e7ee9`).

---

## Seam 1 — MODEL: what the handler validates against, for all 42 tools

Producer: SHIPPED json-schema properties per tool. Consumer: the pydantic model the tool's arguments actually pass through — the manifest's `argument_model` for the 12 type-driven tools, and the handler-side `_*ArgumentsModel` passed to `_validate_mutation_arguments` for the rest. Taught: the schema prose on each property. Trust boundary: the planner's arguments cross into the handler. Live consequence: a property the schema advertises that the model silently drops (a knob that does nothing) or a model field the schema never shows (a hidden knob).

### Task 1.1: Census — derive MODEL per tool from source

**Files:**
- Create: `scripts/cicd/composer_wire_census.py` (the census, reusable by later seams)
- Test: `tests/unit/scripts/test_composer_wire_census.py`

**Interfaces:**
- Produces: `census_model_wire() -> dict[str, ModelWireRow]` where `ModelWireRow` is a frozen dataclass `(tool: str, shipped: frozenset[str], model_class: str | None, model_fields: frozenset[str], site: str)`; `site` is `manifest` / `handler:<module>.<function>` / `none`.

- [ ] **Step 1: Write the failing test that the census sees both loci**

```python
# tests/unit/scripts/test_composer_wire_census.py
from scripts.cicd.composer_wire_census import census_model_wire

def test_type_driven_tools_take_their_model_from_the_manifest() -> None:
    rows = census_model_wire()
    assert rows["set_pipeline"].site == "manifest"
    assert "nodes" in rows["set_pipeline"].model_fields

def test_declarative_tools_take_their_model_from_the_handler() -> None:
    rows = census_model_wire()
    assert rows["upsert_node"].site.startswith("handler:")
    assert rows["upsert_node"].model_class == "_UpsertNodeArgumentsModel"

def test_every_registry_tool_has_a_row() -> None:
    from elspeth.web.composer.tools._dispatch import get_tool_definitions
    assert set(census_model_wire()) == {d["name"] for d in get_tool_definitions()}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/unit/scripts/test_composer_wire_census.py -n 0 -v`
Expected: FAIL with `ModuleNotFoundError: scripts.cicd.composer_wire_census`

- [ ] **Step 3: Write the census**

```python
# scripts/cicd/composer_wire_census.py
"""Composer wire census: derive, per tool, what each wire carries.

Every side is read from the live registry, the live manifest, or the AST of the
handler modules. Nothing here is a regex over source text (AGENTS.md).
"""
from __future__ import annotations

import ast
import importlib
from dataclasses import dataclass
from pathlib import Path

from elspeth.web.composer import redaction
from elspeth.web.composer.tools._dispatch import get_tool_definitions

TOOLS_ROOT = Path(__file__).resolve().parents[2] / "src" / "elspeth" / "web" / "composer" / "tools"
VALIDATOR_NAMES = frozenset({"_validate_mutation_arguments", "_validate_arguments"})


@dataclass(frozen=True)
class ModelWireRow:
    tool: str
    shipped: frozenset[str]
    model_class: str | None
    model_fields: frozenset[str]
    site: str


def _shipped() -> dict[str, frozenset[str]]:
    return {d["name"]: frozenset((d.get("parameters") or {}).get("properties", {})) for d in get_tool_definitions()}


def _handler_models() -> dict[str, tuple[str, str]]:
    """tool -> (model class name, 'handler:<module>.<function>') from the AST.

    A handler is `def _execute_<tool>(args, state, context)`. Its model is the
    Name passed first to a validator call. A validator called with anything
    other than a bare Name is a finding, not a row: raise so the census cannot
    quietly skip it.
    """
    found: dict[str, tuple[str, str]] = {}
    for path in sorted(TOOLS_ROOT.glob("*.py")):
        module = ast.parse(path.read_text(), filename=str(path))
        for node in module.body:
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith("_execute_"):
                continue
            tool = node.name.removeprefix("_execute_")
            for call in ast.walk(node):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                    continue
                if call.func.id not in VALIDATOR_NAMES:
                    continue
                if not call.args or not isinstance(call.args[0], ast.Name):
                    raise RuntimeError(f"{path.name}:{call.lineno}: {call.func.id} called without a bare model Name")
                found[tool] = (call.args[0].id, f"handler:{path.stem}.{node.name}")
    return found


def census_model_wire() -> dict[str, ModelWireRow]:
    shipped = _shipped()
    handler_models = _handler_models()
    rows: dict[str, ModelWireRow] = {}
    for tool, props in shipped.items():
        entry = redaction.MANIFEST[tool]
        if entry.argument_model is not None:
            model = entry.argument_model
            rows[tool] = ModelWireRow(tool, props, model.__name__, frozenset(model.model_fields), "manifest")
            continue
        if tool in handler_models:
            class_name, site = handler_models[tool]
            module_name = site.removeprefix("handler:").rsplit(".", 1)[0]
            model = getattr(importlib.import_module(f"elspeth.web.composer.tools.{module_name}"), class_name)
            rows[tool] = ModelWireRow(tool, props, class_name, frozenset(model.model_fields), site)
            continue
        rows[tool] = ModelWireRow(tool, props, None, frozenset(), "none")
    return rows


if __name__ == "__main__":
    for row in sorted(census_model_wire().values(), key=lambda r: r.tool):
        gap_out = sorted(row.shipped - row.model_fields)
        gap_in = sorted(row.model_fields - row.shipped)
        print(f"{row.tool:32} {row.site:48} shipped-not-model={gap_out} model-not-shipped={gap_in}")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/unit/scripts/test_composer_wire_census.py -n 0 -v`
Expected: PASS ×3. If `_handler_models` raises on a non-Name validator argument, that site is the first finding of the seam: record it on the ticket and decide (fix the handler to pass a bare Name, or teach the census the construct) before continuing.

- [ ] **Step 5: Run the census and record the matrix on the ticket**

Run: `PYTHONPATH=$PWD/src:$PWD/elspeth-lints/src .venv/bin/python scripts/cicd/composer_wire_census.py`
Record: the full table, the count of rows with `site == "none"` (expected to include the 10 zero-knob tools and the 5 knob-bearing no-policy tools), and every non-empty `shipped-not-model` / `model-not-shipped` set, on a new task ticket under `elspeth-54bd0b84cd` titled "Seam 1 MODEL wire census @<sha>".

- [ ] **Step 6: Commit — SUSPENDED** (leave uncommitted; note the two files on the ticket)

### Task 1.2: Verdicts and ratification

- [ ] **Step 1: One verdict per gap row** — for each tool with a non-empty gap, one of: `fix the producer` (schema property with no model field: delete the property or add the field), `fix the consumer` (model field the schema never shows: add the property or drop the field), `fence` (a model field deliberately internal, with the reason), `retire`.
- [ ] **Step 2: LLM charter** — load `yzmir-llm-specialist:using-llm-specialist`; hand it the census table and three real planner tool calls from a session DB (`tool_calls` rows for `upsert_node`, `set_output`, `set_pipeline`); ask whether any `shipped-not-model` knob is one the model has been observed to set.
- [ ] **Step 3: Systems charter** — load `yzmir-systems-thinking:using-systems-thinking`; ask for the shape ledger: every other place a json-schema and a pydantic model describe one payload with no cross-check (start from `guided/` and `mcp/` tool definitions).
- [ ] **Step 4: Walk John through the table**, fences and producer fixes first; record each ruling on the ticket with the date.

### Task 1.3: Pin — extend `schema_contract.py` to every tool

**Files:**
- Modify: `src/elspeth/web/composer/tools/schema_contract.py` (add a general assertion beside the two existing ones)
- Test: `tests/unit/web/composer/test_tool_model_wire_parity.py`

**Interfaces:**
- Produces: `assert_model_wire_compatible(tool: str, *, shipped: frozenset[str], model_fields: frozenset[str], fenced: frozenset[str]) -> None`, raising `RuntimeError` (the module's existing convention: every `assert_*` there raises `RuntimeError` with the path and the reason) naming the tool, the direction, and the keys.

- [ ] **Step 1: Write the failing gate**

```python
# tests/unit/web/composer/test_tool_model_wire_parity.py
"""Every argument knob the planner is shown is a field of the model the handler validates against, and vice versa.

SHIPPED comes from the live registry; MODEL comes from the manifest's argument_model or the
handler's validator call, by AST (scripts/cicd/composer_wire_census.py). Neither side is
hand-listed. Fences live in model_wire_fence.json with a reason each and are themselves gated.
"""
import json
from pathlib import Path

import pytest

from elspeth.web.composer.tools.schema_contract import assert_model_wire_compatible
from scripts.cicd.composer_wire_census import census_model_wire

FENCE = json.loads((Path(__file__).parent / "model_wire_fence.json").read_text())
ROWS = census_model_wire()


@pytest.mark.parametrize("tool", sorted(ROWS))
def test_shipped_and_model_agree(tool: str) -> None:
    row = ROWS[tool]
    if row.site == "none":
        pytest.skip("no model on this wire; covered by the READ seam")
    assert_model_wire_compatible(
        tool, shipped=row.shipped, model_fields=row.model_fields, fenced=frozenset(FENCE.get(tool, {}))
    )


def test_every_fence_row_still_earns_its_place() -> None:
    for tool, keys in FENCE.items():
        row = ROWS[tool]
        for key, reason in keys.items():
            assert reason, f"{tool}.{key}: a fence without a reason"
            assert key in (row.shipped ^ row.model_fields), f"{tool}.{key}: fenced but no longer a gap — retire the fence"


def test_gate_is_not_vacuous() -> None:
    assert sum(1 for r in ROWS.values() if r.site != "none") >= 12
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/unit/web/composer/test_tool_model_wire_parity.py -n 0 -v`
Expected: FAIL with `ImportError: cannot import name 'assert_model_wire_compatible'`

- [ ] **Step 3: Implement the assertion**

```python
# in src/elspeth/web/composer/tools/schema_contract.py, beside assert_upsert_node_schema_compatible
def assert_model_wire_compatible(
    tool: str, *, shipped: frozenset[str], model_fields: frozenset[str], fenced: frozenset[str]
) -> None:
    """Raise unless every shipped knob is a model field and every model field is shipped, fences excepted."""
    shipped_not_model = shipped - model_fields - fenced
    model_not_shipped = model_fields - shipped - fenced
    if shipped_not_model or model_not_shipped:
        raise RuntimeError(
            f"{tool}: advertised but not validated {sorted(shipped_not_model)}; "
            f"validated but never advertised {sorted(model_not_shipped)}"
        )
```

- [ ] **Step 4: Run the gate; every red row is a Task 1.2 verdict** — fix the producer or consumer per the ratified table, or add the fence row `{"<tool>": {"<key>": "<reason>"}}` to `model_wire_fence.json`. Re-run until green with the fence count recorded on the ticket.

- [ ] **Step 5: Mutation ledger** — (a) remove one fence row: gate must fail on that tool; (b) in the census, make `_handler_models` skip validator calls whose first arg is not a Name instead of raising: `test_gate_is_not_vacuous` or a parity row must fail — if neither does, the escape is unguarded and needs a probe site; (c) add a property to one tool's schema in a cp-roundtripped copy: its row must fail. Restore by copying back; reconcile failed + passed against the unmutated total each time.

- [ ] **Step 6: Commit — SUSPENDED**

### Task 1.4: Review to zero, evidence, close

- [ ] Adversarial charter (`red-team` agent) on the gate file and the census; LLM and systems charters at close-out per skill §8; three written verdicts on one named tree state.
- [ ] Evidence: `pytest tests/unit/web/composer tests/unit/scripts -n 6`, then the full suite set-diffed against the baseline set; lint corpus row count vs 1977 with positions masked; `check_contracts.py` census vs 2693/382/63; mypy on `src/elspeth/web/composer/tools` and `scripts/cicd`.
- [ ] Close the seam ticket with the numbers; deliberate residue on its own ticket with a measurement.

---

## Seam 2 — READ: what the handler actually consumes

Producer: SHIPPED. Consumer: for tools with a model, `validated.<field>` attribute reads inside the handler; for tools without one (`site == "none"` rows from Seam 1 that have knobs — measured: `explain_validation_error`, `get_pipeline_state`, `get_plugin_schema`, `get_plugin_assistance`, `list_models`, plus any declarative handler that reads `args[...]` directly), the literal-key subscripts and `.get` calls. Live consequence: a shipped knob nothing reads (SHIPPED not READ — the worst symptom, no error signal).

### Task 2.1: Census — extend `composer_wire_census.py` with `census_read_wire()`

- [ ] **Step 1: Failing test**: `test_read_wire_refuses_dynamic_reads` — a handler that reads `args[key]` with a non-literal key must raise from the census, not be skipped; `test_read_wire_sees_validated_attribute_reads` — `upsert_node`'s row contains `id`, `node_type`, `plugin`, `options`.
- [ ] **Step 2: Implement**: walk each `_execute_<tool>` body; collect `ast.Subscript` on the `args` parameter with `ast.Constant` slices and `args.get("k")` calls (literal first argument), plus `ast.Attribute` reads on the name bound from the validator call (`validated = _validate_mutation_arguments(...)` → the target Name). Any subscript or `.get` on `args` whose key is not an `ast.Constant` raises with the site. Produce `ReadWireRow(tool, shipped, read: frozenset[str], site)`.
- [ ] **Step 3: Matrix on the ticket**: `shipped - read` per tool is the finding list. Verdict per row: `fix the producer` (drop the knob), `fix the consumer` (read it), or `fence` (a knob consumed by a sub-call the AST cannot attribute — name the callee; keep this count near zero).
- [ ] **Step 4: Gate** `tests/unit/web/composer/test_tool_read_wire_parity.py`, same shape as Seam 1's, with `read_wire_fence.json`. Probe sites: a module-level probe handler in the test file exercising the subscript, `.get`, and attribute-read branches, plus one dynamic-key probe asserted to raise.
- [ ] **Step 5: Structural close**: for any handler still reading `args[...]` directly, the ratified fix is a pydantic arguments model registered where Seam 1's census sees it, so the READ wire collapses onto MODEL. Record how many handlers converted.
- [ ] Review, mutation ledger, evidence, close — as Seam 1.

---

## Seam 3 — TAUGHT: every knob is explained to the planner, or fenced

Producer: SHIPPED knobs (104). Taught: the property's own `description` in the json-schema, the tool `description`, and `src/elspeth/web/composer/skills/pipeline_composer.md` / `pipeline_capabilities.md`. Consumer for the same rows: ADMITTED — the 9 knobs on the 5 no-policy tools have no allowlist at all, so this seam also settles what redaction does with them. Live consequence: TAUGHT-not-SHIPPED burns a repair turn against a budget of 2; SHIPPED-not-TAUGHT is a knob the model cannot use on purpose.

### Task 3.1: Census `census_taught_wire()`

- [ ] **Step 1: Failing test**: a knob whose json-schema property has a non-empty `description` counts as taught at the schema; a knob named in backticks in the skill prose counts as taught in the skill; a knob quoted only in ANOTHER tool's description does not count (this is `elspeth-657f603fcd`'s mechanism one wire over — refuse it from the start).
- [ ] **Step 2: Implement**: taught-at-schema from `parameters.properties[k].description`; taught-in-skill by scanning the markdown for `` `k` `` inside the paragraph or table row that names the tool (own-tool context only; reuse `_teaching_gate_support.py`'s quoted-leaf reader for the tokenising, and add the context restriction there so the envelope gate can adopt it — that closes `657f603fcd`).
- [ ] **Step 3: Matrix**: untaught knobs, stale knobs (taught, unshipped), and the 9 no-policy knobs with their ADMITTED disposition.
- [ ] **Step 4: LLM charter FIRST** (skill §4): give it the real tool definitions as the model sees them and the untaught list; it decides which are teach vs fence, and whether each existing description lets the model set the knob correctly from the wire.
- [ ] **Step 5: Gate** `tests/unit/web/composer/test_tool_knob_teaching_gate.py` + `knob_teaching_fence.json`, deriving both sides; the fence is gated. Extend `test_tool_argument_wire_parity.py`'s docstring to name TAUGHT as covered here, so the wire enumeration is complete in one place.
- [ ] **Step 6: ADMITTED for the 5 no-policy tools**: ratified verdict per tool — add a declarative `known_argument_keys` entry (the gate from `test_tool_argument_wire_parity.py` then covers them) or record on the manifest why a discovery tool's arguments are never persisted, with a probe proving they are not.
- [ ] Review, mutation ledger, evidence, close.

---

## Seam 4 — FRONTEND: what the operator is shown

Producer: the redacted argument payload (`redact_tool_call_arguments`, already fixtured in `frontend/src/test/fixtures/redacted-tool-arguments.json`). Consumer: `ProposalDiff.tsx`, `ChatPanel.tsx`, `proposals.py` (the approval card). Live consequence: an operator who opted into explicit approval is shown an empty diff, a false "Changed", a false blast radius, or never sees a widget.

The four open tickets are this seam's matrix; each is an explore-and-pin in miniature.

- [ ] **4.1 `f491fca94e`** — convert `PROJECTED_TOOLS` from transcription to derivation: export `PROJECTED_TOOL_NAMES` from `ProposalDiff.tsx` as the array the switch is built from; a vitest asserts switch-vs-constant parity; `scripts/cicd/bootstrap_proposal_diff_fixture.py` emits that list into the fixture JSON under `projected_tools`, and the Python guard reads it. Mutant: drop a tool from both the TS constant and the fixture → the vitest parity test fails.
- [ ] **4.2 `134b9a9a65`** — route `isEmptyRedactedOptions` through `decodeRedactedOptionSummary(value)?.entryCount === 0`; replace the ChatPanel test's stale third-grammar fixture with a real one from the fixture file. This ENABLES a suppressed widget on the rootless path: per-transition provider-call scrutiny before it lands (AGENTS.md standing trigger), and the LLM charter confirms the widget's behaviour once it can render.
- [ ] **4.3 `7cda5664b0`** — derive `affects` in `proposals.py` from what the handler returns (`_discovery_result` affects nothing in the graph; a blob-store mutation affects the blob store, a value the vocabulary must gain) instead of the constant default; the generic summary gains a blob-store arm naming the blob and the verb. Gate: a parametrised test over every tool asserting the card's `affects` equals the derived set; no per-tool hand branch.
- [ ] **4.4 `d6147d73ed`** — design call for John, not a patch: (a) redact from the raw argument dict so absent keys stay absent; (b) record the provided key set beside the payload; (c) stop comparing nullable optional keys. Present the three with the fixture case `set_pipeline_replaying_current_state` as the driver; implement the ruled option; pin it from the fixture.
- [ ] Review (adversarial + LLM on the card text), mutation ledger, vitest + pytest evidence, close.

---

## Seam 5 — response-side residue from the envelope run

Ten open children of the epic are envelope-side findings the second run recorded rather than fixed. Each is a verdict-and-fix under the existing envelope gate; none needs a new census.

- [ ] `7980efe197` `_failure_result` ships `data.error` / `data.error_code` as a twin of `validation.errors[0]` at ~261 producers — one authority; systems charter names which.
- [ ] `8fe09316ab` `validation_errors` stringified while warnings keep structure — close the type.
- [ ] `72ce6749ac` `_redacted_response_field_N` positional identity — key on the field name.
- [ ] `e12dce8ed6`, `9fcf465c41` envelope census gaps (dotted-path keying; attributed helper in dict-literal position) — fix the census, add the probes.
- [ ] `657f603fcd` — closed by Seam 3's own-tool-context reader.
- [ ] `5e81b50f2e`, `d83095ee87`, `c00e6d9795`, `6aa477c78e`, `9e76d9436b` — verdict each against the envelope matrix; fix in place or fence with a reason.

---

## Seam 6 — scorecard and live trial

- [ ] **Scorecard**: `scripts/cicd/composer_wire_census.py --scorecard` prints one row per tool with a column per wire (SHIPPED count, MODEL site, READ coverage, ADMITTED source, TAUGHT coverage, FRONTEND projected) — every cell derived from the gates' own census functions, no hand entry. A pytest pins that no cell reads "unknown".
- [ ] **Live trial** (skill §10): the composer standard battery (`evals/composer-standard-battery/battery.md`) plus one scenario per seam designed to hit it (a knob previously silently dropped; an approval card under `explicit_approve`), driven through the API detached with a done marker. Count repair turns, tool calls per transition, unknown-key placeholders reaching the model, and approval-card rows. Compare against the peer baseline transcript; a reproduced failure is data.
- [ ] Record the scorecard and trial counts on `elspeth-54bd0b84cd`; close the epic when every tool's row is fully wired or explicitly fenced.

---

## Self-review

- Spec coverage: the six wires each have a seam (SHIPPED is the producer side of every seam; ADMITTED is the landed gate plus Seam 3 step 6 for the 5 uncovered tools; MODEL 1; READ 2; TAUGHT 3; FRONTEND 4); the epic's open children are all slotted or explicitly excluded with a reason.
- Placeholders: Seams 2–6 give instruments, file names, test names and mutation shapes but not full code; that is deliberate because their census does not exist yet and the skill forbids writing a claim ahead of the census. Seam 1 is fully specified and is the template the others repeat.
- Type consistency: `census_model_wire()` / `ModelWireRow` (1.1) are what 1.3 imports; `assert_model_wire_compatible` (1.3) matches its test; it raises `RuntimeError` like every other assertion in `schema_contract.py` (verified 2026-09-08: the module defines no error class of its own).
