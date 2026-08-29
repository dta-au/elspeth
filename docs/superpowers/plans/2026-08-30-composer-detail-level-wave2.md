# Composer Detail Level (Wave 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Carry the landed `show_advanced` flag and the real FieldTier to the remaining eight composer surfaces — tool-call cards, run history & diagnostics, Import YAML, the post-run accounting grid, wire-stage/proposal turns, the plugin catalog, version history — and swap `OptionRows`' static allowlist for catalog-tier-driven ordering.

**Architecture:** No new mechanisms. Every task reads the flag through `useShowAdvanced()` (Wave 1 Task 2) and reuses the Wave 1 idioms: plain summary always in place, technical content in a `<details>` `open={showAdvanced}` or rendered only with the flag, identifiers demoted to `title`/`<code>`. The one wire change is `node_options_summary` gaining a presentational `tier` per pair (row 5, emit-always / admit-optional, tier carried in the server-owned allowlist and pinned to the catalog lowering by test); the one type change is the frontend `PluginSchemaInfo` finally reading the `knob_schema` field the backend already ships (row 8). Row 7 changes the version-history widget from a listbox to a tree so grouping is ARIA-conformant.

**Tech Stack:** FastAPI + Pydantic v2 (backend, `src/elspeth/web`), React 18 + Zustand + vitest/@testing-library + Playwright (frontend, `src/elspeth/web/frontend`), pytest (backend).

**Spec:** Wave 1 plan §"Roadmap: Waves 2 and 3" (`docs/superpowers/plans/2026-08-29-composer-detail-level-wave1.md:1431-end`) — the 8-row Wave 2 table is the scope; the Wave 2 scope memo (`.superpowers/sdd/2026-08-29-composer-detail-level-wave1/wave2-scope-memo.md`) carries the binding rulings; epic `elspeth-cd8abcba3f`; tickets `elspeth-af559a0bab`, `elspeth-34e810312c`, `elspeth-aa39cffb16`, `elspeth-05a240b82a`, `elspeth-ca456d9d8d`, `elspeth-8555a6a9e0`, `elspeth-c8a402a9a4`, `elspeth-a6ea581e8a` (follow-up half). Every file:line below was re-verified against branch head `acf7040e0` (2026-08-30); the round-1 review reports live beside the memo (`wave2-review-{reality,quality,architecture}.md`).

## Global Constraints

- **Read `docs/agents/recent-code-hints.md` §"Whole-tree gates" before touching code.** No new `getattr`/`hasattr` anywhere (attribute-contracts + masquerade gates scan the whole tree, tests included). Owned types get direct attribute access; parse only genuine Tier-3 boundaries via the file's existing idioms (`isinstance(x, Mapping)` membership + index, per ADR-032).
- **Trust-tier lint corpus is fail-closed and must not grow.** Task 0 captures the before-corpus at the branch point, before any lane lands; Task 9 diffs the after-capture against it; the diff must add nothing. Never hand-edit a `judge_metadata_signature`; never shape code around signature churn. (Verified for this wave: `config/cicd/enforce_tier_model/*.yaml` carries no entry for `web/composer/guided/protocol.py`, so Task 5 cannot drift a signed binding.)
- **Composer invariants:** no server-side authoring of pipeline structure; **no tutorial-special paths** (ADR-031). Every disclosure applies identically in tutorial mode.
- **Audit-required elements stay visible regardless of `show_advanced`:** AuthorityChip, Audit panel rows + Blocks-run/Advisory legend, Run-confirm egress lines, tool-outcome ribbon prefixes, acknowledgement cards, completion honesty gate, "Validation passed · N checks" headline. Wave 2 additions to that list, from the tickets: the evidential prefix vocabulary Applied / Looked up / Completed / Ran / Attempted (not applied) / Failed / Cancelled (elspeth-f5e6723133), the accounting-corruption badge (elspeth-d5578ccd98), the audit-closure verdict line and the missing/duplicate-terminal integrity warnings, the run history Cancel affordance, the wire-stage blocker panel (elspeth-3b35abf148), and every version remaining revertable — to the version the user selected — in the history dropdown.
- **Debug mode expands disclosures; it never adds surfaces.** Every item hidden when the flag is off has a plain summary in its place. A surface opened with the flag on must close (or become closeable) when the flag goes off — Task 6's Schema panel is the worked case.
- **`<details open={showAdvanced}>` is uncontrolled after the first user toggle.** This is the Wave 1 idiom (`OptionRows.tsx:129`): the `open` prop only re-applies when it *changes*, so a user who toggled a disclosure by hand and the flag can disagree until the next flag flip. Tests that assert "opens when the flag flips on a mounted tree" exercise the prop change, which is the contract; do not "fix" this by making the elements controlled.
- **Every flag reader goes through `useShowAdvanced()`** (`@/stores/preferencesStore`); the preferences panel is the only direct store reader.
- **`knob_schema` extras are read via `_composer_extras()`** (MappingProxyType-aware); never `type(extra) is dict`.
- **Goldens regenerate only in a task that owns them.** No Wave 2 task regenerates `tests/golden/web/catalog/knob_schema/*.json` — no task changes the catalog lowering output (all 685 fields across the 55 goldens already carry `tier`). If a run shows them dirty, stop: something out of scope changed.
- **If a task edits `src/elspeth/plugins/*`: re-pin `source_file_hash` LAST** via `scripts/cicd/plugin_hash.py` in the same task (propagates into `docs/architecture/dag/scenario-corpus/v1/manifest.yaml`). No Wave 2 task touches plugins; this is the tripwire in case one drifts into it.
- **New TSX class names need real stylesheet rules** — `src/styles/classNames.test.ts` is a whole-tree gate (Wave 1 lesson, follow-up 1d2504e2f). The directory gates (`catalogClassNames.test.ts`, `executionClassNames.test.ts`) check every class a component in that directory *applies* against the whole stylesheet barrel and separately assert their `RULE_LESS_BY_DESIGN` names have NO rule. Verified for this wave: none of the classes added below appears in either no-rule allowlist, so those gate files are not edited.
- **Test files touching a Zustand store must reset it** — a top-level `beforeEach(() => resetStore(useXStore))` (from `@/test/store-helpers`) in every test file whose component becomes a flag reader or a catalog-store reader in this wave (Wave 1 found silent state leakage masking branch coverage). The files this wave turns into readers are named in each task; the reset is a numbered step, not a "verify".
- **The `af559a0bab` tool list derives from the registry, never from grep:** `from elspeth.web.composer.tools._dispatch import _REGISTERED_TOOLS` (a tuple of `ToolDeclaration`; 40 tools). Verified for this plan and independently by all three reviewers: 15 registered tools have no `toolCallDescriptions.ts` entry — `create_blob, delete_blob, get_blob_content, get_blob_metadata, inspect_source, list_blobs, list_composer_blobs, list_secret_refs, set_source_from_blob, set_source_from_blobs, splice_transform, update_blob, validate_secret_ref, wire_blob_inline_ref, wire_secret_ref` — and `profile_prevalidation_out` is not registered. The ticket text already carries this corrected list (R1's "fix the ticket if it still carries the wrong list" is satisfied; Task 1 re-verifies and only comments).
- **Durable-session replay (Task 5).** Guided turns and proposal projections are durable `sessions.db` rows and are re-validated on load through three gates: the protocol validators (`_node_options_summary_error`), the frontend decoder, and the audit-projection verifier `verify_guided_proposal_projection` (`planning.py:3948-3968`, whole-payload strict equality against a fresh re-derivation). The first two admit the old `{key, value}` pair; the third cannot be relaxed and is handled by accept-and-prove (Task 5 preamble). No `SESSION_SCHEMA_EPOCH` bump: the change is additive and admit-optional on the read paths.
- **Copy register:** sentence case, no internal identifiers in visible text; raw identifiers go in `title`/`data-*` or a `<code>`/mono secondary span. A `<details>` is NOT a firewall for regex-based DOM pins — its children still land in `textContent`.
- **The per-PR default-DOM acceptance pin is ONE helper, used by every task.** Task 1 creates `src/test/defaultDomPins.ts` exporting `expectNoIdentifiersInDefaultDom(container, { allowSelectors? })`: it asserts that, with the flag off, neither the visible `textContent` (after removing `<code>`, `.tool-call-ribbon-name`, `.tool-call-info-bubble-name`, and any caller-supplied `allowSelectors`) nor any `aria-label` (except the `ToolCallInfo` trigger's "What does <name> do?" — an identifier surface by design) contains a UUID, a 32+-hex hash, or a `[a-z]+_[a-z_]+` snake_case token. `title` attributes are explicitly allowed to carry identifiers. Each task's default-DOM test calls this helper; the reviewer runs those tests first.
- **Shared checkout:** stage only your own pathspecs; never `git restore`/`clean` files you did not stage; no `git stash` (hook-blocked). Full `pytest tests/` is a background job in a worktree — cap parallelism at `-n 12` when other agents are running. Never run two Playwright commands concurrently in one worktree (auth state is worktree-global).
- Frontend commands run from `src/elspeth/web/frontend`: `npx vitest run <path>`; `npm run lint` is defined (`package.json:17`, eslint over `src` plus the named e2e files) and is not conditional; backend from repo root with `source .venv/bin/activate`.
- **Sequencing rule (Wave 1 roadmap):** one PR per ticket; each PR's default-DOM regression pin (the shared helper above) is the acceptance test the reviewer runs first.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/elspeth/web/frontend/src/test/defaultDomPins.ts` (create) | the shared default-DOM acceptance helper |
| `src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts` (modify) | +15 entries (7 read-only, 8 mutating); export `toolCallOutcomeLabelParts` |
| `src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx` (modify) | sentence primary, raw name mono secondary on ribbon + proposal heading |
| `src/elspeth/web/frontend/src/components/chat/ProposalDiff.tsx:530-537` (modify) | no-arguments copy |
| `src/elspeth/web/frontend/src/components/header/versionLabels.ts` (modify) | `deriveVersionLabel` returns the sentence; `describeVersionOperation` → `versionOperationIdentifier`; new `versionLabelKind` |
| `tests/unit/web/composer/test_tool_call_description_parity.py` (create) | registry ⊆ map + kind/half parity, from the live `_REGISTERED_TOOLS` |
| `src/elspeth/web/frontend/src/components/execution/RunsHistoryDrawer.tsx` (modify) | UUID/id-list/raw-`<pre>` gating; curated failures rendered at one site |
| `src/elspeth/web/frontend/src/components/composer/CompletionBar.tsx:96` (modify) | Import YAML behind the flag |
| `src/elspeth/web/frontend/src/components/catalog/UnavailableComponentRow.tsx` (create) | shared disabled-component row (ImportYamlModal + CatalogDrawer) |
| `src/elspeth/web/frontend/tests/e2e/helpers/api.ts`, `helpers/workspace-assertions.ts`, `composer-workspace-geometry.spec.ts` (modify) | e2e seeds `show_advanced` where the Import button is measured |
| `src/elspeth/web/frontend/src/components/execution/runTerminalPhrases.ts` (modify) | closure phrases + glosses (single register owner) |
| `src/elspeth/web/frontend/src/components/execution/ProgressView.tsx` (modify) | closure sentence + glossary, grid + errors feed behind disclosures |
| `src/elspeth/web/composer/guided/protocol.py` (modify) | allowlist carries `plugin → {key: tier}`; `node_options_summary` emits tier; validator admits optional tier |
| `src/elspeth/web/catalog/knob_schema.py:61` (modify) | `KnobField.tier` NotRequired → Required (deferred Wave 1 minor) |
| `tests/unit/web/catalog/test_guided_option_tier_parity.py` (create) | allowlist tiers == catalog lowering tiers, via the public `get_schema` |
| `src/elspeth/web/frontend/src/api/guidedDecoder.ts:198-207` + `src/types/guided.ts:718-721` (modify) | optional tier on the wire type + decoder |
| `src/elspeth/web/frontend/src/components/chat/guided/behaviorSummary.ts` (create) | `behaviorSummary`/`gateSummary` moved out of ProposePipelineTurn for reuse |
| `src/elspeth/web/frontend/src/components/chat/guided/optionTiers.ts` (create) | `optionTier(entry)` — absent tier reads as "common" |
| `src/elspeth/web/frontend/src/components/chat/guided/WireStageTurn.tsx` (modify) | per-row Technical details; display names; tiered option pairs |
| `src/elspeth/web/frontend/src/components/chat/guided/ProposePipelineTurn.tsx` (modify) | display-name subtitles/components list; tiered option pairs |
| `src/elspeth/web/frontend/src/components/chat/guided/SingleSelectTurn.tsx:39-60` (modify) | uploaded-time disambiguator |
| `src/elspeth/web/frontend/src/components/chat/guided/SchemaFormTurn.tsx` (tests only) | all-advanced case + `within(details)` ancestry (deferred Wave 1 minors) |
| `src/elspeth/web/frontend/src/components/catalog/FilterChipStrip.tsx` + `PluginCard.tsx` + `auditCharacteristics.ts` (modify) | chip/strip/Schema gating; 3-column schema fields |
| `src/elspeth/web/frontend/src/components/header/versionGrouping.ts` (create) | pure grouping of consecutive edited versions, keyed on `versionLabelKind` |
| `src/elspeth/web/frontend/src/components/header/HeaderVersionSelector.tsx` (modify) | `role="tree"` history with expandable group items; selection keyed by version number |
| `src/elspeth/web/frontend/src/test/composerFixtures.ts` (modify) | v11–v19 version-history fixture |
| `src/elspeth/web/frontend/src/types/index.ts:436-441` (modify) | `PluginSchemaInfo.knob_schema` |
| `src/elspeth/web/frontend/src/components/inspector/OptionRows.tsx` (modify) | catalog-tier-driven ordering; value-bound masking; explicit fallback list |
| `src/elspeth/web/frontend/src/components/inspector/GraphView.tsx` + `workspace/PipelineSpecView.tsx` (modify) | pass `plugin` to OptionRows |
| Area CSS: `chat/chat.css`, `execution/execution.css`, `catalog/catalog.css`, `header/header.css`, `guided/guided.css` (modify) | rules for every new class |

---

### Task 0: Preflight (hub, before any lane starts)

**Files:** none in the tree.

- [ ] **Step 1: Capture the lint corpus at the branch point**

```bash
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules all --root src/elspeth > /tmp/claude-1000/-home-john-elspeth/w2-lints-before.txt; grep -c . /tmp/claude-1000/-home-john-elspeth/w2-lints-before.txt  # grep -c prints 0 and exits 1 on zero matches — do not wrap in set -e
```

Record the count and the HEAD sha beside it. This must happen before Task 1 lands, or another lane's churn is attributed to this wave.

- [ ] **Step 2: Unblock the tracker**

The tracker still shows the Wave 1 blockers open (`elspeth-9c11df65f8` = building, `elspeth-9cca900d41` = fixing, `elspeth-27efd1e801` = fixing), so `filigree show` reports six of the eight Wave 2 tickets as `Blocked by:` and only `af559a0bab`/`a6ea581e8a` as ready. The memo says the dependencies are satisfied in code (Wave 1 merge-ready at e6dc2da1b + follow-ups). The hub closes the three Wave 1 tickets with a comment naming the landing commits (`filigree add-comment <id> "..."`; bugs go `fixing → verifying → close`, the feature `building → close`), then confirms `filigree show` on each Wave 2 ticket reads `Ready: YES`. Lanes then claim with `filigree start-work <id> --assignee <lane>`.

---

### Task 1: Tool-call cards — sentence primary, 15 tools mapped, registry parity (`elspeth-af559a0bab`, roadmap row 1)

**Files:**
- Modify: `src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts` (the two map literals, `:17-44` read-only, `:46-73` mutating; private `outcomeLabelParts` `:143`)
- Modify: `src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.test.ts` (`EXPECTED_READ_ONLY` `:25-40`)
- Modify: `src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx` (ribbon `:85-118`, proposal heading `:121-127`)
- Modify: `src/elspeth/web/frontend/src/components/chat/ToolCallCard.test.tsx`, `src/components/chat/chat.css`
- Modify: `src/elspeth/web/frontend/src/components/chat/ProposalDiff.tsx:530-537`, `ProposalDiff.test.tsx`
- Modify: `src/elspeth/web/frontend/src/components/header/versionLabels.ts` (`describeVersionOperation` `:199-213`, `deriveVersionLabel` `:230-253`), `versionLabels.test.ts`, `src/components/header/HeaderVersionSelector.tsx:14,:282` (import rename only)
- Create: `src/elspeth/web/frontend/src/test/defaultDomPins.ts`
- Create: `tests/unit/web/composer/test_tool_call_description_parity.py`

**Interfaces:**
- Produces: `toolCallOutcomeLabelParts(name, outcome): ToolCallLabelParts` (export of the existing private `outcomeLabelParts`), 15 new `TOOL_CALL_DESCRIPTIONS` entries, `deriveVersionLabel` returning `Applied: <sentence>` for mapped applied tools, `versionOperationIdentifier(version, messages): string | null` (renamed `describeVersionOperation`, now the raw `Applied: <name>` for the `title`), and the shared pin helper `expectNoIdentifiersInDefaultDom`. Task 7 consumes `deriveVersionLabel`.
- The read-only/mutating split is the honesty contract: DISCOVERY-kind registry tools go in the read-only half, MUTATION-kind in the mutating half. Registry ground truth (verified by import): 7 of the 15 are discovery (`get_blob_content, get_blob_metadata, inspect_source, list_blobs, list_composer_blobs, list_secret_refs, validate_secret_ref`), 8 are mutation (`create_blob, delete_blob, update_blob, set_source_from_blob, set_source_from_blobs, splice_transform, wire_blob_inline_ref, wire_secret_ref`). The kind-vs-half check already holds for the 25 mapped registered tools, so the parity test goes green on the existing tree once the 15 are added.

- [ ] **Step 1: Write the failing Python parity test**

Create `tests/unit/web/composer/test_tool_call_description_parity.py` (modelled on `tests/unit/web/catalog/test_audit_characteristic_vocabulary_parity.py`):

```python
"""Registry↔TypeScript parity for composer tool-call descriptions.

The web dispatch registry (``_dispatch._REGISTERED_TOOLS``) is the source of
truth for which tools exist. ``toolCallDescriptions.ts`` must carry a
humanised sentence for every registered tool — an unmapped name falls to the
generic "Composer tool call." and, after elspeth-af559a0bab, ships a raw
snake_case primary label. The read-only / mutating halves must also agree
with the registry's ToolKind, because "Looked up:" is an honesty claim
(elspeth-f5e6723133): a DISCOVERY tool missing from the read-only half loses
its lookup label; a MUTATION tool added to it would fabricate a read.

The TS map may carry MORE names than the web registry (MCP-only session
tools such as ``generate_yaml`` and ``load_session``); the subset assertions
below are deliberate.
"""

from __future__ import annotations

import re
from pathlib import Path

import elspeth
from elspeth.web.composer.tools._dispatch import _REGISTERED_TOOLS

_PACKAGE_ROOT = Path(elspeth.__file__).parent
_TS_PATH = _PACKAGE_ROOT / "web" / "frontend" / "src" / "components" / "chat" / "toolCallDescriptions.ts"

_READ_ONLY_BLOCK_RE = re.compile(
    r"const READ_ONLY_TOOL_CALL_DESCRIPTIONS[^=]*=\s*\{(.*?)\n\};", re.DOTALL
)
_MUTATING_BLOCK_RE = re.compile(
    r"const MUTATING_TOOL_CALL_DESCRIPTIONS[^=]*=\s*\{(.*?)\n\};", re.DOTALL
)
# Prettier-stable record form: two-space indent, bare snake_case key, colon.
_KEY_RE = re.compile(r"^\s{2}([a-z][a-z0-9_]*):", re.MULTILINE)

# Post-change sizes (14 + 7 read-only, 17 + 8 mutating). Pinned as floors so a
# half silently emptied by a bad edit fails here even if the subset tests
# above still pass for the names that remain.
_MIN_READ_ONLY = 21
_MIN_MUTATING = 25


def _ts_halves() -> tuple[set[str], set[str]]:
    text = _TS_PATH.read_text(encoding="utf-8")
    read_only = _READ_ONLY_BLOCK_RE.search(text)
    mutating = _MUTATING_BLOCK_RE.search(text)
    assert read_only is not None, f"READ_ONLY map literal not found in {_TS_PATH.name}"
    assert mutating is not None, f"MUTATING map literal not found in {_TS_PATH.name}"
    return set(_KEY_RE.findall(read_only.group(1))), set(_KEY_RE.findall(mutating.group(1)))


def test_every_registered_tool_has_a_description() -> None:
    read_only, mutating = _ts_halves()
    mapped = read_only | mutating
    registered = {tool.name for tool in _REGISTERED_TOOLS}
    missing = registered - mapped
    assert not missing, (
        f"Registered tools with no toolCallDescriptions.ts entry: {sorted(missing)}. "
        "Add an audience-facing sentence to the correct half (kind-matched, see below)."
    )


def test_registry_kind_agrees_with_the_read_only_split() -> None:
    read_only, mutating = _ts_halves()
    for tool in _REGISTERED_TOOLS:
        if tool.kind.name.endswith("DISCOVERY"):
            assert tool.name in read_only, (
                f"{tool.name} is {tool.kind.name} but not in the read-only half — "
                "it would lose its honest 'Looked up' label."
            )
        else:
            assert tool.kind.name.endswith("MUTATION"), f"unclassified ToolKind: {tool.kind}"
            assert tool.name in mutating, (
                f"{tool.name} is {tool.kind.name} but not in the mutating half — "
                "the read-only map must never absorb a durable write."
            )


def test_ts_halves_keep_their_post_change_size() -> None:
    read_only, mutating = _ts_halves()
    assert len(read_only) >= _MIN_READ_ONLY and len(mutating) >= _MIN_MUTATING, (
        f"Too few keys matched ({len(read_only)}/{len(mutating)}) — a half was emptied, "
        "or the record shape / regex has drifted."
    )
```

- [ ] **Step 2: Run to verify it fails**

Run: `source .venv/bin/activate && pytest tests/unit/web/composer/test_tool_call_description_parity.py -q`
Expected: all three FAIL — the first lists exactly the 15 names; the kind test fails on the same names; the size test sees 14/17.

- [ ] **Step 3: Add the 15 entries**

In `toolCallDescriptions.ts`, append to `READ_ONLY_TOOL_CALL_DESCRIPTIONS` (after `generate_yaml`):

```ts
  get_blob_content: "Reads the content of an uploaded file for inspection.",
  get_blob_metadata: "Looks up an uploaded file's name, size, and status.",
  inspect_source:
    "Reads structural facts about an uploaded file — headers and sample rows — to plan the source.",
  list_blobs: "Lists the files uploaded or created in this session.",
  list_composer_blobs:
    "Lists the ready files available for audited inline authoring.",
  list_secret_refs:
    "Lists the credential references available to this session — names only, never values.",
  validate_secret_ref:
    "Checks that a credential reference exists and is accessible.",
```

Append to `MUTATING_TOOL_CALL_DESCRIPTIONS` (after `delete_session`):

```ts
  create_blob: "Creates a new file from inline content for the pipeline to use.",
  delete_blob: "Deletes an uploaded file and its storage.",
  update_blob: "Replaces the content of an uploaded file.",
  set_source_from_blob: "Wires an uploaded file as the pipeline's data source.",
  set_source_from_blobs:
    "Wires one or more uploaded files as a one-row-per-file data source.",
  splice_transform: "Inserts a transform between two connected steps.",
  wire_blob_inline_ref:
    "Pins an uploaded file's content into a configuration field by hash.",
  wire_secret_ref:
    "Places a credential reference in the pipeline configuration, resolved at run time.",
```

Update `toolCallDescriptions.test.ts` `EXPECTED_READ_ONLY` (`:25-40`) — add the 7 discovery names. The structural `MUTATING_VERBS` check (`:66-74`, `/^(set|upsert|patch|remove|delete|splice|apply|clear|create|update|wire)_/`) passes as-is: none of the 7 starts with a listed verb (`validate_` is deliberately absent — leave the regex alone). The existing durable-mutation tests (`Completed: create_blob` etc.) keep passing: those names land in the mutating half.

- [ ] **Step 4: Run the Python parity test and the TS oracle**

Run: `pytest tests/unit/web/composer/test_tool_call_description_parity.py -q` → PASS.
Run (from `src/elspeth/web/frontend`): `npx vitest run src/components/chat/toolCallDescriptions.test.ts` → PASS.
Per-task lint-corpus diff (memo: mandatory for every backend-touching task, so a finding is attributable to THIS lane, not discovered at closeout): `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules all --root src/elspeth > /tmp/claude-1000/-home-john-elspeth/w2-lints-task1.txt; diff /tmp/claude-1000/-home-john-elspeth/w2-lints-before.txt /tmp/claude-1000/-home-john-elspeth/w2-lints-task1.txt` → empty (a `tests/`-only Python addition cannot move a `src/elspeth` corpus; the run is the proof, not the reasoning).

- [ ] **Step 5: Commit the vocabulary**

```bash
git add src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.test.ts tests/unit/web/composer/test_tool_call_description_parity.py
git commit -m "feat(chat): map all 15 unmapped web-registry tools; registry-parity test (elspeth-af559a0bab)"
```

- [ ] **Step 6: Create the shared default-DOM pin helper**

`src/test/defaultDomPins.ts`:

```ts
// ============================================================================
// expectNoIdentifiersInDefaultDom — the ONE executable form of the Wave 1/2
// acceptance rule: with show_advanced off, no UUID, no 32+-hex hash, and no
// snake_case identifier reaches visible text or an aria-label. Identifier
// surfaces by design are excluded here, once, so every task's pin agrees on
// what "visible" means: <code>, the mono tool-name secondary, the tool
// tooltip's identifier heading, and the tooltip trigger's "What does X do?"
// aria-label. `title` attributes are not inspected — they are where raw
// identifiers are allowed to live.
// ============================================================================

import { expect } from "vitest";

const UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
const HEX32_RE = /\b[0-9a-f]{32,}\b/i;
const SNAKE_RE = /\b[a-z]+_[a-z_]+\b/;

const IDENTIFIER_SURFACES = [
  "code",
  ".tool-call-ribbon-name",
  ".tool-call-info-bubble-name",
] as const;

export function expectNoIdentifiersInDefaultDom(
  container: HTMLElement,
  options: { allowSelectors?: readonly string[] } = {},
): void {
  const clone = container.cloneNode(true) as HTMLElement;
  for (const selector of [...IDENTIFIER_SURFACES, ...(options.allowSelectors ?? [])]) {
    clone.querySelectorAll(selector).forEach((el) => el.remove());
  }
  const text = clone.textContent ?? "";
  expect(text).not.toMatch(UUID_RE);
  expect(text).not.toMatch(HEX32_RE);
  expect(text).not.toMatch(SNAKE_RE);
  for (const el of container.querySelectorAll("[aria-label]")) {
    const label = el.getAttribute("aria-label") ?? "";
    if (/^What does .* do\?$/.test(label)) continue; // ToolCallInfo trigger
    expect(label).not.toMatch(UUID_RE);
    expect(label).not.toMatch(HEX32_RE);
    expect(label).not.toMatch(SNAKE_RE);
  }
}
```

- [ ] **Step 7: Write the failing card tests**

`ToolCallCard.test.tsx` has no tool-call factory — the file uses a module-level literal (`const toolCall: ToolCall` at `:9-15`, name `set_pipeline`) and a describe-local `call(outcome?, appliedStateVersion?)` at `:81-92` that hardcodes `upsert_node`. Add a new module-level factory near `:16`:

```tsx
function makeToolCall(
  name: string,
  overrides: { outcome?: ToolCall["outcome"]; applied_state_version?: number | null } = {},
): ToolCall {
  return {
    id: `call-${name}`,
    type: "function",
    function: { name, arguments: "{}" },
    ...(overrides.outcome !== undefined ? { outcome: overrides.outcome } : {}),
    ...(overrides.applied_state_version !== undefined
      ? { applied_state_version: overrides.applied_state_version }
      : {}),
  };
}
```

and a new describe block (imports: add `expectNoIdentifiersInDefaultDom` from `@/test/defaultDomPins`):

```tsx
describe("ToolCallCard humanised primary label (elspeth-af559a0bab)", () => {
  it("renders the sentence as the ribbon primary and the raw name as mono secondary", () => {
    render(
      <ToolCallCard
        toolCall={makeToolCall("upsert_node", { outcome: "applied", applied_state_version: 3 })}
        proposal={null}
        onAccept={vi.fn()}
        onReject={vi.fn()}
      />,
    );
    expect(
      screen.getByText(
        "Applied: Adds a new transform or gate node, or replaces an existing one with the same id.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("upsert_node", { selector: "code" })).toBeInTheDocument();
    expect(screen.getByText("v3")).toBeInTheDocument();
  });

  it("keeps the rejected qualifier attached to the sentence", () => {
    render(
      <ToolCallCard
        toolCall={makeToolCall("upsert_node", { outcome: "rejected" })}
        proposal={null}
        onAccept={vi.fn()}
        onReject={vi.fn()}
      />,
    );
    expect(
      screen.getByText(
        "Attempted: Adds a new transform or gate node, or replaces an existing one with the same id. (not applied)",
      ),
    ).toBeInTheDocument();
  });

  it("falls back to the raw-name label for an unknown tool (no dishonest sentence)", () => {
    render(
      <ToolCallCard
        toolCall={makeToolCall("mystery_tool")}
        proposal={null}
        onAccept={vi.fn()}
        onReject={vi.fn()}
      />,
    );
    expect(screen.getByText("Ran: mystery_tool")).toBeInTheDocument();
  });

  it("default DOM of a settled card passes the shared identifier pin", () => {
    const { container } = render(
      <ToolCallCard
        toolCall={makeToolCall("set_source_from_blob", { outcome: "applied", applied_state_version: 7 })}
        proposal={null}
        onAccept={vi.fn()}
        onReject={vi.fn()}
      />,
    );
    expectNoIdentifiersInDefaultDom(container);
  });
});
```

Run: `npx vitest run src/components/chat/ToolCallCard.test.tsx -t "humanised"` → FAIL (labels are still `Applied: upsert_node`).

- [ ] **Step 8: Implement the card**

`toolCallDescriptions.ts` — export the settled-outcome parts function (keep the string-returning wrappers verbatim):

```ts
export function toolCallOutcomeLabelParts(
  name: string,
  outcome: ToolCall["outcome"],
): ToolCallLabelParts {
  return outcomeLabelParts(name, outcome);
}
```

`ToolCallCard.tsx` proposal-less branch — replace `const label = toolCallOutcomeLabel(...)` and `<span>{label}</span>` with:

```tsx
    const { prefix, qualifier } = toolCallOutcomeLabelParts(
      toolCall.function.name,
      outcome,
    );
    // Sentence primary, identifier secondary — the ComposingIndicator ruling
    // (ComposingIndicator.tsx:320-346) applied to the settled ribbon. The
    // evidential prefix stays attached to the PRIMARY line: a failed mutation
    // rendered as a bare sentence would claim the mutation happened. An
    // unmapped name has no honest sentence (describeToolCall's fallback is
    // generic), so it keeps the raw-name label and no secondary.
    const sentence: string | undefined =
      TOOL_CALL_DESCRIPTIONS[toolCall.function.name];
```

and in the JSX, in place of `<span>{label}</span>`:

```tsx
        <span className="tool-call-ribbon-text">
          {prefix}: {sentence ?? toolCall.function.name}
          {qualifier}
        </span>
        {sentence !== undefined && (
          <code className="tool-call-ribbon-name">{toolCall.function.name}</code>
        )}
```

Add `TOOL_CALL_DESCRIPTIONS` and `toolCallOutcomeLabelParts` to the import from `./toolCallDescriptions` (keep `describeToolCall` for the tooltip; drop `toolCallOutcomeLabel` if now unused).

Proposal heading (`:121-127`) — same treatment:

```tsx
  const proposalSentence: string | undefined =
    TOOL_CALL_DESCRIPTIONS[proposal.tool_name];
  const headingPrefix =
    proposal.status === "pending"
      ? "Proposed"
      : proposal.status === "committed"
        ? "Applied"
        : "Rejected";
  const heading = `${headingPrefix}: ${proposalSentence ?? proposal.tool_name}`;
```

and in the header JSX, after `<strong>{heading}</strong>`:

```tsx
        {proposalSentence !== undefined && (
          <code className="tool-call-ribbon-name">{proposal.tool_name}</code>
        )}
```

`chat.css` — next to the existing `.tool-call-ribbon-version` rule (`--font-mono` is `tokens.css:229`; `--color-text-secondary` is already used in this file):

```css
.tool-call-ribbon-text {
  min-width: 0;
}
.tool-call-ribbon-name {
  font-family: var(--font-mono);
  font-size: 0.78rem;
  color: var(--color-text-secondary);
}
```

`ProposalDiff.tsx:532-536` — change the empty-arguments copy:

```tsx
      <p className="tool-call-arg-empty" data-testid="proposal-arg-fields">
        No settings change in this step.
      </p>
```

Update the pre-existing `ToolCallCard.test.tsx` string pins — exact new values:

| Line | Old | New |
|---|---|---|
| `:46` | `Proposed: set_pipeline` | `Proposed: Replaces the entire pipeline configuration in a single operation.` |
| `:74`, `:214` | `Looked up: get_pipeline_state` | `Looked up: Reads the current pipeline state being composed in this session.` |
| `:103`, `:117` | `Applied: upsert_node` | `Applied: Adds a new transform or gate node, or replaces an existing one with the same id.` |
| `:131` | `Attempted: upsert_node (not applied)` | `Attempted: Adds a new transform or gate node, or replaces an existing one with the same id. (not applied)` |
| `:145` | `Failed: upsert_node` | `Failed: Adds a new transform or gate node, or replaces an existing one with the same id.` |
| `:157` | `Cancelled: upsert_node` | `Cancelled: Adds a new transform or gate node, or replaces an existing one with the same id.` |
| `:174` | `Ran: upsert_node` | `Ran: Adds a new transform or gate node, or replaces an existing one with the same id.` |
| `:195` | `Completed: create_blob` | `Completed: Creates a new file from inline content for the pipeline to use.` |

Grep `ProposalDiff.test.tsx` for `"This tool call takes no arguments."` and update to the new copy.

- [ ] **Step 9: Version labels reuse the sentence; the tooltip carries the identifier**

`versionLabels.ts` — rename `describeVersionOperation` (`:199-213`) to `versionOperationIdentifier` with a truthful docstring and body:

```ts
/**
 * The raw applied-tool identifier behind this version, as "Applied: <name>",
 * or null when the version has no applied tool-call stamp. The visible row
 * carries the audience-facing sentence (deriveVersionLabel); this is the
 * `title` for operators who need the exact tool name (elspeth-af559a0bab).
 */
export function versionOperationIdentifier(
  version: CompositionStateVersion,
  messages: ChatMessage[],
): string | null {
  const name = appliedToolCallName(version, messages);
  return name === null ? null : `Applied: ${name}`;
}
```

In `deriveVersionLabel` (`:230-253`) the applied arm becomes:

```ts
  const appliedName = appliedToolCallName(version, messages);
  if (appliedName !== null) {
    const sentence = TOOL_CALL_DESCRIPTIONS[appliedName];
    return sentence !== undefined ? `Applied: ${sentence}` : `Applied: ${appliedName}`;
  }
```

(`TOOL_CALL_DESCRIPTIONS` is already imported at `versionLabels.ts:1`.) `HeaderVersionSelector.tsx:14` imports and `:282` calls `describeVersionOperation` — rename both to `versionOperationIdentifier` (no other consumer: grepped). `versionLabels.test.ts`: the `describeVersionOperation` block (`:126-163`) is renamed and now expects `Applied: set_pipeline`-style raw strings (unknown tool still yields the raw name; unlabeled version still null); `deriveVersionLabel` applied tests (`:165`, `:202`) expect the sentence (e.g. `"Applied: Adds or replaces a connection between two nodes in the pipeline."` for `upsert_edge`; `"Applied: Updates one or more configuration options on a transform or gate node."` for `patch_node_options`). `HeaderVersionSelector.test.tsx` pins only `edited` / `session created` names (verified) and is not touched by this task.

- [ ] **Step 10: Run the affected frontend suites**

Run: `npx vitest run src/components/chat src/components/header`
Expected: PASS. Then `npx tsc --noEmit -p .` → clean.

- [ ] **Step 11: Commit**

```bash
git add src/elspeth/web/frontend/src/test/defaultDomPins.ts src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx src/elspeth/web/frontend/src/components/chat/ToolCallCard.test.tsx src/elspeth/web/frontend/src/components/chat/ProposalDiff.tsx src/elspeth/web/frontend/src/components/chat/ProposalDiff.test.tsx src/elspeth/web/frontend/src/components/chat/chat.css src/elspeth/web/frontend/src/components/header/versionLabels.ts src/elspeth/web/frontend/src/components/header/versionLabels.test.ts src/elspeth/web/frontend/src/components/header/HeaderVersionSelector.tsx
git commit -m "feat(chat): humanised sentence is the tool-card primary label; raw name demoted to mono secondary; shared default-DOM pin (elspeth-af559a0bab)"
```

**Ticket disposition:** CLOSE at end of wave (Task 9). The ticket's tool list was already corrected to the registry-derived 15; the closing comment records the verification instead of an edit.

---

### Task 2: Run history & diagnostics behind the flag (`elspeth-34e810312c`, roadmap row 2)

**Files:**
- Modify: `src/elspeth/web/frontend/src/components/execution/RunsHistoryDrawer.tsx` (run-id span `:316`, corruption badge `:330-339`, Cancel aria-label `:347`, Show/Hide-detail aria-label `:357-361`, `RunDiagnosticsPanel` `:433-560` — failure blocks `:475-489`, operations `:492-501`, tokens `:502-530`, `RunStateFailureDetail` `:203-224`)
- Test: `src/elspeth/web/frontend/src/components/execution/RunsHistoryDrawer.test.tsx`

The ticket's line citations are exact (the file has not changed since); the Show/Hide-detail buttons (`:357-361`) also embed `run.id` in their aria-labels — not cited by the ticket, but covered by its acceptance ("including aria-label attributes"), so they change too.

**Interfaces:**
- Consumes: `useShowAdvanced()`, `expectNoIdentifiersInDefaultDom`.
- Keeps unchanged: Cancel behaviour, Show/Hide detail toggle, corruption badge (audit-required), Refresh/Explain buttons, `RunStateFailureDetail` (a curated closed-identifier surface with the authored S3 hint) — now rendered at ONE site regardless of the flag.

- [ ] **Step 1: Write the failing tests**

Append to `RunsHistoryDrawer.test.tsx` (add `import { usePreferencesStore } from "@/stores/preferencesStore";`, `import { resetStore } from "@/test/store-helpers";`, `import { expectNoIdentifiersInDefaultDom } from "@/test/defaultDomPins";`, and `act` to the `@testing-library/react` import). Reuse the file's `makeDiagnostics(overrides)` (`:23-90`) and its run-literal convention (`{ id, status } as never`, `:96-98`):

```tsx
describe("detail level (elspeth-34e810312c)", () => {
  const UUID_RUN_ID = "f976fd8b-4432-4f8f-bbc3-2d8a9f2114e0";
  const uuidRun = (status: string, extra: Record<string, unknown> = {}) =>
    ({ id: UUID_RUN_ID, status, started_at: "2026-08-29T10:00:00Z", ...extra }) as never;

  beforeEach(() => resetStore(usePreferencesStore));

  it("keeps the UUID out of visible text and aria-labels with the flag off, but in the label title", () => {
    const { container } = render(
      <RunsHistoryDrawer onClose={vi.fn()} runsOverride={[uuidRun("running")]} />,
    );
    expectNoIdentifiersInDefaultDom(container);
    expect(screen.getByText(/^Run 1 · /)).toHaveAttribute("title", UUID_RUN_ID);
    expect(screen.getByRole("button", { name: /^Cancel Run 1 · / })).toBeInTheDocument();
  });

  it("shows the UUID span when show_advanced is on", () => {
    usePreferencesStore.setState({ showAdvanced: true });
    render(<RunsHistoryDrawer onClose={vi.fn()} runsOverride={[uuidRun("completed")]} />);
    expect(screen.getByText(UUID_RUN_ID)).toBeInTheDocument();
  });

  it("gates the token/operation lists and raw failure <pre> behind the flag; keeps count, Explain, and the curated failure detail", async () => {
    useExecutionStore.setState({
      loadRunDiagnostics: vi.fn().mockResolvedValue(undefined),
      diagnosticsByRunId: {
        [UUID_RUN_ID]: makeDiagnostics({
          run_id: UUID_RUN_ID,
          tokens: [
            {
              ...makeDiagnostics().tokens[0],
              states: [{ ...makeDiagnostics().tokens[0].states[0], error: { code: "some_code" } }],
            },
          ],
        }),
      },
    } as never);
    render(<RunsHistoryDrawer onClose={vi.fn()} runsOverride={[uuidRun("failed")]} />);
    await userEvent.click(screen.getByRole("button", { name: /^Show detail for Run 1/ }));
    expect(screen.getByText(/1 token/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Explain" })).toBeInTheDocument();
    const drawer = screen.getByRole("dialog", { name: "Pipeline runs" });
    expect(drawer.querySelector(".run-diagnostics-tokens")).toBeNull();
    expect(drawer.querySelector(".run-diagnostics-operations")).toBeNull();
    expect(screen.queryByTestId("run-failure-detail")).not.toBeInTheDocument();
    expect(drawer.textContent).not.toMatch(/token-1|state-1/);
    // Curated authored surface stays (closed identifiers + authored hint).
    expect(screen.getByTestId("run-state-failure-state-1")).toBeInTheDocument();
    act(() => usePreferencesStore.setState({ showAdvanced: true }));
    expect(drawer.querySelector(".run-diagnostics-tokens")).not.toBeNull();
    expect(screen.getByTestId("run-failure-detail")).toBeInTheDocument();
    // One render site: still exactly one curated failure row with the flag on.
    expect(screen.getAllByTestId("run-state-failure-state-1")).toHaveLength(1);
  });

  it("keeps the accounting-corruption badge regardless of the flag", () => {
    render(
      <RunsHistoryDrawer
        onClose={vi.fn()}
        runsOverride={[uuidRun("completed", { accounting_corruption: { violations: ["duplicate terminal outcome"] } })]}
      />,
    );
    expect(screen.getByText("⚠ audit accounting corrupt")).toBeInTheDocument();
  });
});
```

(`makeDiagnostics()`'s first token is `token-1`, its state `state-1` on node `rate_colours`, `error: null` — the override above gives it a `visibleStateFailure`-shaped error so `RunStateFailureDetail` renders.)

- [ ] **Step 2: Run to verify failure**

Run: `npx vitest run src/components/execution/RunsHistoryDrawer.test.tsx -t "detail level"` → FAIL (UUID visible; token list rendered).

- [ ] **Step 3: Implement**

`RunsHistoryDrawer.tsx`:

1. `import { useShowAdvanced } from "@/stores/preferencesStore";`. Call `const showAdvanced = useShowAdvanced();` in BOTH `RunsHistoryDrawer` (row rendering) and `RunDiagnosticsPanel` (the panel is a plain function component in the same file — the hook is the selector path; no prop threading).
2. Run row (`:315-316`): label span gains the title; id span gates:

```tsx
                    <span className="runs-history-item-label" title={run.id}>
                      {runLabel}
                    </span>
                    {showAdvanced && (
                      <span className="runs-history-item-id">{run.id}</span>
                    )}
```

3. Aria-labels: `:347` → `` aria-label={`Cancel ${runLabel}`} ``; `:357-361` → `` `${expandedRunId === run.id ? "Hide" : "Show"} detail for ${runLabel}` ``.
4. `RunDiagnosticsPanel`: wrap the operations block (`:492-501`), the tokens block (`:502-530`, including its inline `RunStateFailureDetail` map at `:518-524` — DELETE that inline map), and both raw failure blocks (`run-failure-detail` `:475-482`, `run-stored-failure-detail` `:484-489`) in `showAdvanced && (...)`. Render the curated per-state failures at ONE site, unconditionally, directly after the `{error && …}` alert:

```tsx
      {diagnostics?.tokens.flatMap((token) =>
        token.states
          .filter((state) => state.status === "failed")
          .map((state) => (
            <RunStateFailureDetail
              key={`${state.state_id}-failure`}
              error={state.error}
              nodeId={state.node_id}
              stateId={state.state_id}
            />
          )),
      )}
```

The flag-on token list now shows ids/states only; the curated failure rows sit above it in both modes, so an audit-required element has exactly one renderer. `state.node_id` is a node id, not a UUID/`state_id` — it stays (acceptance bans UUIDs and `token_id`/`state_id` strings; the pin helper's snake_case check runs on the flag-off DOM where `rate_colours`-style node ids would trip it — the diagnostics fixture's node id is `rate_colours`, so the third test above does NOT call the helper; the first test does, on a drawer with no diagnostics loaded. Resolving node ids through the phrase map is Task 4's instruction for the progress feed, not this ticket's).
5. Do not touch: `aria-controls={`run-history-diagnostics-${run.id}`}` (an id-ref wiring attribute, not a label; the helper inspects `aria-label` only), the corruption badge, Cancel/confirm flow, `buildPendingWorkingView`.

- [ ] **Step 4: Update the pre-existing pins this breaks, then run**

Exact sites in `RunsHistoryDrawer.test.tsx`:
- `:118` `expect(screen.getByText(/r2/))` — pins the visible id span; set `usePreferencesStore.setState({ showAdvanced: true })` at the top of that test (the suite's top-level `beforeEach` gains `resetStore(usePreferencesStore)` so it does not leak).
- `:292`, `:307`, `:325`, `:341` `getByRole("button", { name: /cancel run r2/i })` → `/^Cancel Run \d+ · /` (the ordinal label; the fixture runs have no `started_at`, so `labelRun` renders `Time unavailable` — match on the `Cancel Run` prefix).
- `:354` `show detail for r2` → `/^Show detail for Run/`; `:361` `getByText("token-1")` and `:359` `run-failure-detail` are flag-gated now — set the flag on in that test (`:346-364`) and in any later test asserting token/operation rows or `run-stored-failure-detail` (`:493-535` region).

Run: `npx vitest run src/components/execution/RunsHistoryDrawer.test.tsx` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/web/frontend/src/components/execution/RunsHistoryDrawer.tsx src/elspeth/web/frontend/src/components/execution/RunsHistoryDrawer.test.tsx
git commit -m "feat(execution): run history hides UUIDs and raw diagnostics behind show_advanced; corruption badge, Explain and curated failures stay (elspeth-34e810312c)"
```

**Ticket disposition:** CLOSE at end of wave.

---

### Task 3: Import YAML behind the flag (`elspeth-aa39cffb16`, roadmap row 3)

**Files:**
- Modify: `src/elspeth/web/frontend/src/components/composer/CompletionBar.tsx:96`, `CompletionBar.test.tsx`
- Create: `src/elspeth/web/frontend/src/components/catalog/UnavailableComponentRow.tsx`
- Modify: `src/elspeth/web/frontend/src/components/sidebar/ImportYamlModal.tsx:1080-1099`, `src/elspeth/web/frontend/src/components/catalog/CatalogDrawer.tsx:565-593` (+ `CatalogDrawer.test.tsx` and the ImportYamlModal test file if they pin the raw `<code>{plugin_id}</code>`)
- Modify (e2e): `src/elspeth/web/frontend/tests/e2e/helpers/api.ts`, `tests/e2e/helpers/workspace-assertions.ts:19-22,:128-135`, `tests/e2e/composer-workspace-geometry.spec.ts:309-312,:733-745,:812-825,:895-908`

**Interfaces:**
- Consumes: `useShowAdvanced()`, `pluginDisplayName` (`@/components/catalog/pluginDisplayName`).
- Produces: `UnavailableComponentRow({ finding, reasonLabel, actions }): JSX.Element` — the shared `<li>` body both surfaces render; e2e helper `setShowAdvanced(ctx, value)`.
- **Decision (aria-labels on the row's buttons):** display name in the `aria-label`, raw id in `title` — matching the row body and the acceptance pin (snake_case plugin ids in aria-labels would trip it). Applied identically in both files.
- Unchanged: YAML tab Copy/Download; command palette Export YAML; the 2026-08-15 Run-emphasis decisions.
- **e2e reality:** no e2e spec sets the preference today, so every spec runs at the default (flag OFF). Four sites require the Import button at default: the geometry spec's bar-height test (`:733-745`), its two alignment tests (`:812-825`, `:895-908`) and `expectPrimaryControlsInViewport` (`workspace-assertions.ts:128-135`, called once at `composer-workspace-geometry.spec.ts:309-312`). The page-object locator (`tests/e2e/page-objects/composer-page.ts:133`) is unchanged.

- [ ] **Step 1: Write the failing bar test**

Append to `CompletionBar.test.tsx` (add the preferences-store import + `resetStore`; add `resetStore(usePreferencesStore)` to the file's top-level `beforeEach`):

```tsx
  it("hides Import YAML with the default detail level — exactly two completion gestures", () => {
    useSessionStore.setState({ activeSessionId: "sess-1" });
    useExecutionStore.setState({
      validationResult: _validValidation(),
      isExecuting: false,
      progress: null,
      execute: vi.fn(),
    });
    render(<CompletionBar />);
    const bar = screen.getByTestId("completion-bar");
    expect(within(bar).getAllByRole("button").map((b) => b.textContent)).toEqual([
      "Save for review",
      "Run pipeline",
    ]);
  });
```

(The two-name pin is sound: the existing `:100-125` test already pins `["Save for review", "Import YAML", "Run pipeline"]` via the same `getAllByRole("button").map(textContent)`, proving `ExecuteButton`'s text is exactly `Run pipeline` and no other button lives in the bar.) Then flip every existing Import-YAML-dependent test in this file (`:100-125` order test, `:250-278` axis test, `:333` click test) to set `usePreferencesStore.setState({ showAdvanced: true })` first. `ChatPanel.test.tsx:3702` and `:4994` assert the button's absence in their contexts and stay green.

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run src/components/composer/CompletionBar.test.tsx` → new test FAILS (three buttons).

- [ ] **Step 3: Implement the gate**

`CompletionBar.tsx` — `import { useShowAdvanced } from "@/stores/preferencesStore";`, `const showAdvanced = useShowAdvanced();` beside the other hooks, and `:96`:

```tsx
      {showAdvanced && <ImportYamlButton />}
```

Add to the header comment: Import YAML is the hand-edit/re-import surface for a technical audience (elspeth-aa39cffb16) and renders only with the show_advanced preference; export (YAML tab Copy/Download, Ctrl+Shift+Y) stays for everyone.

- [ ] **Step 4: Share the unavailable-component row and humanise its plugin id**

Create `src/components/catalog/UnavailableComponentRow.tsx` (no `key` here — React reads `key` from the element the parent's `.map()` creates):

```tsx
// ============================================================================
// UnavailableComponentRow — the ONE row for a disabled/unavailable saved
// component, shared by ImportYamlModal's preflight list and CatalogDrawer's
// "Unavailable saved components" section so the two cannot drift
// (elspeth-aa39cffb16). The authored component id is the actionable name and
// stays; the plugin renders by display name with the raw id demoted to a
// title attribute (copy register). Callers supply `key` on this element.
// ============================================================================

import type { ReactNode } from "react";

import { pluginDisplayName } from "./pluginDisplayName";

export interface UnavailableComponentFindingLike {
  component_id: string;
  plugin_id: string;
  reason_code: string;
}

export function UnavailableComponentRow({
  finding,
  reasonLabel,
  actions,
}: {
  finding: UnavailableComponentFindingLike;
  reasonLabel: string;
  actions: ReactNode;
}): JSX.Element {
  return (
    <li className="validation-banner-error-item">
      <div>
        <strong>{finding.component_id}</strong>{" "}
        <span title={finding.plugin_id}>{pluginDisplayName(finding.plugin_id)}</span>{" "}
        — {reasonLabel}
      </div>
      <div className="import-yaml-actions">{actions}</div>
    </li>
  );
}
```

In `CatalogDrawer.tsx:565-593` and `ImportYamlModal.tsx:1080-1099`, each `policyFindings.map((finding) => (<li …>…</li>))` becomes:

```tsx
                {policyFindings.map((finding) => (
                  <UnavailableComponentRow
                    key={`${finding.component_id}:${finding.plugin_id}`}
                    finding={finding}
                    reasonLabel={unavailableReasonLabel(finding.reason_code)}
                    actions={
                      <>
                        <Button
                          className="btn-small"
                          aria-label={`Remove disabled component ${finding.component_id} (${pluginDisplayName(finding.plugin_id)})`}
                          title={finding.plugin_id}
                          onClick={() => handleRemoveDisabled(finding)}
                        >
                          Remove
                        </Button>
                        …the surface's existing second button (CatalogDrawer's Replace, copied verbatim from :582-588 with the same aria-label/title treatment; ImportYamlModal has only Remove — keep its existing single button)…
                      </>
                    }
                  />
                ))}
```

(Each file keeps its own `unavailableReasonLabel` import and click handlers; add `pluginDisplayName` to each file's imports.) The catalog directory gate checks classes *applied* in `components/catalog/` against the whole stylesheet barrel: `validation-banner-error-item` and `import-yaml-actions` are defined there (they already render from `CatalogDrawer.tsx`), so no gate record changes. Update any test pins on the raw `<code>{plugin_id}</code>` in `CatalogDrawer.test.tsx` / the ImportYamlModal tests to the display-name + `title` contract.

- [ ] **Step 5: e2e — seed the preference where the Import button is measured**

`tests/e2e/helpers/api.ts` — add beside `createSession`:

```ts
/** Detail level (elspeth-aa39cffb16): Import YAML renders only when the
 *  show_advanced preference is on; specs that measure the three-verb bar
 *  seed it, and reset it, through this helper. */
export async function setShowAdvanced(
  ctx: APIRequestContext,
  value: boolean,
): Promise<void> {
  const resp = await ctx.patch("/api/composer-preferences", {
    data: { show_advanced: value },
  });
  if (!resp.ok()) {
    throw new Error(
      `PATCH /api/composer-preferences failed (${resp.status()}): ${(await resp.text()).slice(0, 500)}`,
    );
  }
}
```

`workspace-assertions.ts:19-22` — `WorkspaceControlCapabilities` gains `importYaml: boolean;` and `:128-135` becomes:

```ts
  const completionControls: Array<[Locator, boolean]> = [
    [composer.saveForReview(), capabilities.completion],
    [composer.runPipeline(), capabilities.completion],
    [composer.importYaml(), capabilities.completion && capabilities.importYaml],
  ];
  for (const [control, present] of completionControls) {
    await expect(control).toHaveCount(present ? 1 : 0);
    if (present) await expectControlReachable(control);
  }
```

The single caller (`composer-workspace-geometry.spec.ts:309-312`) adds `importYaml: false` (default preference; the scenario runner does not seed it). The three Import-measuring tests (`:733-745`, `:812-825`, `:895-908`) seed the preference for the duration of the test: obtain the API context the spec already uses for session setup (`authedContext(tokenFromStorageState(...))` — copy the pattern from the spec's own `installWorkspaceScenario`/`createSession` usage), call `await setShowAdvanced(ctx, true)` before `composer.goto(sessionId)`, and `await setShowAdvanced(ctx, false)` in the `finally`. Those three tests then measure the technical-mode three-verb bar, which is what their alignment assertions describe; nothing else in the suite sees the flag. `npm run lint` covers these e2e files by name (`package.json:17`).

- [ ] **Step 6: Run**

Run: `npx vitest run src/components/composer/CompletionBar.test.tsx src/components/catalog/CatalogDrawer.test.tsx src/components/sidebar` → PASS. Then, sequentially in this worktree: `npx playwright test tests/e2e/composer-workspace-geometry.spec.ts` (the local stack must be up — see Task 9 Step 1b for the invocation) → PASS.

- [ ] **Step 7: Commit**

```bash
git add src/elspeth/web/frontend/src/components/composer/CompletionBar.tsx src/elspeth/web/frontend/src/components/composer/CompletionBar.test.tsx src/elspeth/web/frontend/src/components/catalog/UnavailableComponentRow.tsx src/elspeth/web/frontend/src/components/catalog/CatalogDrawer.tsx src/elspeth/web/frontend/src/components/catalog/CatalogDrawer.test.tsx src/elspeth/web/frontend/src/components/sidebar/ImportYamlModal.tsx src/elspeth/web/frontend/tests/e2e/helpers/api.ts src/elspeth/web/frontend/tests/e2e/helpers/workspace-assertions.ts src/elspeth/web/frontend/tests/e2e/composer-workspace-geometry.spec.ts
git commit -m "feat(composer): Import YAML renders only with show_advanced; shared unavailable-component row; e2e seeds the preference where the button is measured (elspeth-aa39cffb16)"
```

(Add the ImportYamlModal test file to the pathspec only if Step 4 edited it.)

**Ticket disposition:** CLOSE at end of wave.

---

### Task 4: Post-run accounting grid + recent-errors summary (`elspeth-05a240b82a`, roadmap row 4)

**Files:**
- Modify: `src/elspeth/web/frontend/src/components/execution/runTerminalPhrases.ts` (beside `RUN_ACCOUNTING_LABELS` `:97-107`)
- Modify: `src/elspeth/web/frontend/src/components/execution/ProgressView.tsx` (`ProgressAccountingDetails` `:59-112`, quarantined `:281-295`, recent errors `:315-346`)
- Modify: `src/elspeth/web/frontend/src/components/execution/execution.css`
- Test: `src/elspeth/web/frontend/src/components/execution/ProgressView.test.tsx`

**Interfaces:**
- Consumes: `useShowAdvanced()`; `makePhraseFor(compositionState)` from `@/lib/validationHumaniser` (`:162`) + `useSessionStore((s) => s.compositionState)` — the same phrase map ValidationResult uses (the `elspeth-27efd1e801` dependency).
- Produces (in `runTerminalPhrases.ts`, the single register owner per `ProgressView.tsx:60-62` / elspeth-406b503a82): `RUN_ACCOUNTING_CLOSURE_PHRASES`, `RUN_ACCOUNTING_GLOSSES`.
- Unchanged: the "Queued — waiting to start / Running — counts so far" caption (R2-F5), StatusBadge glyphs (elspeth-e1c5ad0b53), the live-region announcements. The closure verdict and the missing/duplicate-terminal warnings are audit-closure evidence and stay visible regardless of the flag.

- [ ] **Step 1: Write the failing tests**

`ProgressView.test.tsx` mocks `useWebSocket` and builds `RunProgress` literals through `progressFixture(overrides)` (`:13-27`). Append (add imports: `usePreferencesStore`, `useSessionStore`, `resetStore`, `makeComposition` from `@/test/composerFixtures`, `within` from `@testing-library/react`, `expectNoIdentifiersInDefaultDom`):

```tsx
function mountProgress(overrides: Record<string, unknown>) {
  (useWebSocket as ReturnType<typeof vi.fn>).mockReturnValue({
    activeRunId: "run-1",
    wsDisconnected: false,
    progress: progressFixture(overrides),
  });
  return render(<ProgressView />);
}

function accounting(integrity: Partial<{ closure: string; missing_terminal_outcomes: number; duplicate_terminal_outcomes: number }>) {
  return {
    source: { rows_processed: 1, rows_rejected: 0, rows_read: 1 },
    tokens: { emitted: 4, terminal: 4, succeeded: 4, failed: 0, structural: 0, pending: 0, abandoned: 0 },
    routing: { routed_success: 0, routed_failure: 0, quarantined: 0, discarded: 0 },
    integrity: { closure: "closed", missing_terminal_outcomes: 0, duplicate_terminal_outcomes: 0, ...integrity },
  };
}

describe("detail level (elspeth-05a240b82a)", () => {
  beforeEach(() => {
    resetStore(usePreferencesStore);
    useSessionStore.setState({ compositionState: null } as never);
  });

  it("keeps the closure verdict visible and collapses the six-cell grid by default", () => {
    const { container } = mountProgress({ status: "completed", accounting: accounting({}) });
    expect(screen.getByText("Audit closure: complete — every row is accounted for.")).toBeInTheDocument();
    const detail = screen.getByText("Accounting detail").closest("details") as HTMLElement;
    expect(detail).not.toBeNull();
    expect(detail).not.toHaveAttribute("open");
    expect(within(detail).getByText("Tokens emitted")).toBeInTheDocument();
    expectNoIdentifiersInDefaultDom(container);
  });

  it("opens the grid when show_advanced is on, and keeps integrity warnings out of the disclosure", () => {
    usePreferencesStore.setState({ showAdvanced: true });
    mountProgress({ status: "completed", accounting: accounting({ closure: "open", missing_terminal_outcomes: 2 }) });
    expect(screen.getByText("Accounting detail").closest("details")).toHaveAttribute("open");
    expect(screen.getByText("Missing terminal").closest("details")).toBeNull();
  });

  it("glosses quarantined rows", () => {
    mountProgress({ status: "completed", tokens_quarantined: 3 });
    expect(screen.getByText("3 quarantined", { selector: "span" })).toHaveAttribute(
      "title",
      "Quarantined rows are kept in the audit trail but excluded from the output.",
    );
  });

  it("summarises the recent-errors feed to a count and resolves node ids in the disclosure", () => {
    useSessionStore.setState({ compositionState: makeComposition(2) } as never);
    mountProgress({
      status: "failed",
      tokens_failed: 2,
      recent_errors: [
        { node_id: "select_columns", message: "boom", row_id: "r-1" },
        { node_id: "select_columns", message: "boom again", row_id: "r-2" },
      ],
    });
    const details = screen.getByText("2 rows failed — view details").closest("details") as HTMLElement;
    expect(details).not.toBeNull();
    expect(details).not.toHaveAttribute("open");
    expect(within(details).queryAllByText(/^select_columns$/)).toHaveLength(0);
  });
});
```

(`makeComposition(2)` yields a composition whose second node is `select_columns` — the same fixture Wave 1 Task 8 used with the phrase map; check `composerFixtures.ts:119` and pick the node id the fixture actually carries.)

- [ ] **Step 2: Run to verify failure**

Run: `npx vitest run src/components/execution/ProgressView.test.tsx -t "detail level"` → FAIL.

- [ ] **Step 3: Implement**

`runTerminalPhrases.ts` — beside `RUN_ACCOUNTING_LABELS` (`:97-107`), the same owner:

```ts
/** Closure verdict sentences (elspeth-05a240b82a). One owner for the
 *  sentence-case register (elspeth-406b503a82); ProgressView imports these. */
export const RUN_ACCOUNTING_CLOSURE_PHRASES: Record<RunAccountingIntegrity["closure"], string> = {
  closed: "complete — every row is accounted for.",
  open: "incomplete — some rows are not yet accounted for.",
  abandoned: "closed with abandoned rows — some rows were marked permanently undecidable.",
  unknown: "not verified for this run.",
};

export const RUN_ACCOUNTING_GLOSSES = {
  token: "A token is one row's journey through the pipeline.",
  quarantined: "Quarantined rows are kept in the audit trail but excluded from the output.",
} as const;
```

(`RunAccountingIntegrity` from `@/types/index`, `:714-715`: `closure: "closed" | "open" | "abandoned" | "unknown"`.)

`ProgressView.tsx`:

1. Imports: `useShowAdvanced`, `useSessionStore`, `makePhraseFor`, `useMemo`; extend the `runTerminalPhrases` import with the two new exports.
2. `ProgressAccountingDetails` gains a `showAdvanced: boolean` prop. New shape — visible block first:

```tsx
      <p className="progress-accounting-closure">
        {RUN_ACCOUNTING_LABELS.auditClosure}: {RUN_ACCOUNTING_CLOSURE_PHRASES[accounting.integrity.closure]}
      </p>
```

then the existing `progress-accounting-integrity` warnings block (unchanged, still `value > 0`-gated — visible always; drop the old bare `Audit closure: <value>` item from it since the sentence replaces it), then the six-cell `<dl>` moves inside:

```tsx
      <details className="progress-accounting-detail" open={showAdvanced}>
        <summary title={RUN_ACCOUNTING_GLOSSES.token}>Accounting detail</summary>
        <dl className="progress-accounting-grid">…existing counters…</dl>
      </details>
```

3. Quarantined chip (`:291-293`): `<span title={RUN_ACCOUNTING_GLOSSES.quarantined}>{progress.tokens_quarantined.toLocaleString()} quarantined</span>`.
4. Recent errors (`:315-346`): inside `ProgressView`, `const showAdvanced = useShowAdvanced();`, `const compositionState = useSessionStore((s) => s.compositionState);`, `const phraseFor = useMemo(() => makePhraseFor(compositionState), [compositionState]);`. Replace the block with:

```tsx
      {progress.recent_errors.length > 0 && (
        <details className="progress-errors" open={showAdvanced}>
          <summary className="progress-errors-title">
            {progress.recent_errors.length === 1
              ? "1 row failed — view details"
              : `${progress.recent_errors.length} rows failed — view details`}
          </summary>
          <div className="progress-errors-container">
            {progress.recent_errors.map((err, i) => (
              <div key={`${err.node_id}-${i}`} className="progress-error-item" style={…existing border style…}>
                {err.node_id && <strong>{phraseFor(err.node_id)}</strong>}
                {err.node_id && ": "}
                {err.message}
                {err.row_id && (
                  <span className="progress-error-row-id"> (row: {err.row_id})</span>
                )}
              </div>
            ))}
          </div>
        </details>
      )}
```

(`makePhraseFor(compositionState)` returns `(componentId: string) => string` — Wave 1 Task 8's usage.) The live-region announcements (`buildStatusAnnouncement`) are untouched.
5. `execution.css` — rules on the bare classes AND their summaries (the directory gate enumerates `.progress-accounting*` / `.progress-errors-*` owners at `execution.css:5-6`; add the new names to that header comment):

```css
.progress-accounting-closure {
  margin: 0 0 0.25rem;
  font-weight: 600;
}
.progress-accounting-detail {
  margin-top: 0.25rem;
}
.progress-accounting-detail > summary {
  cursor: pointer;
  color: var(--color-text-secondary);
}
.progress-errors {
  margin-top: 0.5rem;
}
.progress-errors > summary {
  cursor: pointer;
  font-weight: 600;
}
```

- [ ] **Step 4: Update the pre-existing pins, then run**

`ProgressView.test.tsx:127-178` asserts `getByText("Audit closure")` and `getByText("closed")` — replace with `getByText("Audit closure: complete — every row is accounted for.")`; its `Tokens emitted` etc. pins still hold (closed `<details>` children are in the DOM). Any other test pinning `Recent errors (` changes to the `rows failed — view details` summary.

Run: `npx vitest run src/components/execution` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/web/frontend/src/components/execution/runTerminalPhrases.ts src/elspeth/web/frontend/src/components/execution/ProgressView.tsx src/elspeth/web/frontend/src/components/execution/ProgressView.test.tsx src/elspeth/web/frontend/src/components/execution/execution.css
git commit -m "feat(execution): closure verdict sentence + glossary from the run-phrase owner; accounting grid and error feed behind disclosures (elspeth-05a240b82a, elspeth-406b503a82)"
```

**Ticket disposition:** CLOSE at end of wave.

---

### Task 5: Wire-stage / proposal turns — technical facts under Technical details; `node_options_summary` through tiers (`elspeth-ca456d9d8d`, roadmap row 5)

**Design decisions (recorded here because they bind the implementation; flagged to the operator in the drafting report):**

1. **Tier authority seam.** The tier for each allowlisted option pair lives IN the server-owned allowlist (`_NODE_OPTION_SUMMARY_ALLOWLIST` becomes `plugin → {key: tier}`), not in a separate table and not derived from the catalog at call time. Why not derive: `node_options_summary` is called from two projections (`emitters.py:768`, `planning.py:3811`) that must agree byte-for-byte, and from the audit verifier's re-derivation (`planning.py:3948-3968`) — `_build_projection` receives only `catalog_plugin_ids: Mapping[str, frozenset[str]]` (`:3554-3562`), no schemas, and plumbing a catalog handle through the verifier path is a Wave-3-sized change for a value that is `"common"` on every allowlisted key today. (The earlier "layering" justification was wrong — `protocol.py:16` already imports catalog types; the real constraint is the verifier's catalog-less re-derivation.) The allowlist stays the single authority for *which* keys and *what tier*, and a parity test in the catalog suite pins those tiers to the lowering through the public `get_schema`. Cost if wrong: a plugin annotating an allowlisted knob `advanced` turns that parity test red until the allowlist entry follows — a one-line, test-guarded edit.
2. **Replay posture: emit-always, admit-optional, verifier accept-and-prove.** Fresh projections always carry `tier`; `_node_options_summary_error` and the frontend decoder admit `{key, value}` without it (absent → `"common"`). The audit-projection verifier (`verify_guided_proposal_projection`) is whole-payload strict equality and cannot be relaxed without weakening an audit control; it is handled by proof: only `field_mapper` nodes ever produce summary pairs, and a read-only sweep of the local `data/sessions.db` finds zero rows containing `node_options_summary` (reviewer sweep 2026-08-30 across all 22 tables; Step 2 repeats it as a gate). No `SESSION_SCHEMA_EPOCH` bump (`sessions/models.py:239`, epoch 47): the change is additive and admit-optional on every read path; `receipt_contracts.py:173`'s `semantics_only_changes` label is not extended. Cost if wrong: a deployment holding a durable field_mapper proposal projection raises `AuditIntegrityError` on reload — fail-closed and visible, recovered by DB recreation under the pre-release posture; the sweep is the guard.

**Files:**
- Modify: `src/elspeth/web/composer/guided/protocol.py` (`_NODE_OPTION_SUMMARY_ALLOWLIST` `:855-857`, `_NodeOptionSummary` `:862-864`, `node_options_summary` + its `@trust_boundary` `:910-945`, `public_node_option_keys` `:948-958`, `_node_options_summary_error` `:960-985`)
- Modify: `src/elspeth/web/catalog/knob_schema.py:61` (`tier: NotRequired[FieldTier]` → `tier: FieldTier`)
- Test: `tests/unit/web/composer/guided/test_protocol.py`; create `tests/unit/web/catalog/test_guided_option_tier_parity.py`
- Test (equality pins on emitter output): `tests/unit/web/composer/guided/test_emitters.py` (`:661`, `:677`), `tests/integration/web/composer/guided/test_proposal_audit_projection.py` (`:237-257`)
- Modify: `src/elspeth/web/frontend/src/types/guided.ts:718-721`, `src/api/guidedDecoder.ts:198-207`
- Create: `src/elspeth/web/frontend/src/components/chat/guided/behaviorSummary.ts`, `src/components/chat/guided/optionTiers.ts`
- Modify: `src/elspeth/web/frontend/src/components/chat/guided/WireStageTurn.tsx` (rows `:425-507`), `ProposePipelineTurn.tsx` (subtitles `:277,:283,:289`, components list `:407-433`), `SingleSelectTurn.tsx:39-60`, `ChatPanel.tsx:1223-1290`, `types/guided.ts:224-228`, `guided/guided.css`
- Modify (same defect, Spec tab + inspector — live-check addendum): `src/elspeth/web/frontend/src/components/workspace/PipelineSpecView.tsx:104-107` (the `Plugin` `<dd>{row.plugin}</dd>`), `src/elspeth/web/frontend/src/components/inspector/GraphView.tsx:697-698` (`<p className="graph-config-plugin">{config.plugin}</p>`)
- Test: `WireStageTurn.test.tsx`, `ProposePipelineTurn.test.tsx`, `SingleSelectTurn.test.tsx`, `SchemaFormTurn.test.tsx`, `src/api/guidedDecoder.test.ts`

**Interfaces:**
- Produces (wire): `node_options_summary` pairs become `{key, value, tier}`, `tier ∈ {"essential","common","advanced"}`; validators accept the old `{key, value}`; the frontend treats absent tier as `"common"`.
- Produces (frontend): `behaviorSummary`/`gateSummary` from `guided/behaviorSummary.ts`; `optionTier(entry)` from `guided/optionTiers.ts`; `GuidedSourceBlobCandidate.createdAt: string`.
- Consumes: `useShowAdvanced()`; `stepLabelForPlugin` (`@/components/chat/interpretationStepLabel`, already imported by WireStageTurn; `"field_mapper"` → `"Output"`, `interpretationStepLabel.ts:41`).

- [ ] **Step 1: Failing backend tests**

`tests/unit/web/composer/guided/test_protocol.py` — its import block (`:10-27`) does not import the two functions; extend it:

```python
from elspeth.web.composer.guided.protocol import (
    _NODE_OPTION_SUMMARY_ALLOWLIST,
    _NODE_OPTION_SUMMARY_RENDERERS,
    _node_options_summary_error,
    node_options_summary,
    ...existing names...
)
```

Append:

```python
def test_node_options_summary_carries_a_tier_per_pair() -> None:
    summary = node_options_summary("field_mapper", {"mapping": {"a": "b"}, "select_only": True})
    assert [entry["key"] for entry in summary] == ["mapping", "select_only"]
    for entry in summary:
        assert entry["tier"] in ("essential", "common", "advanced")


def test_node_options_summary_error_admits_the_pre_tier_pair_shape() -> None:
    # Durable sessions written before the tier landed replay through this
    # validator; {key, value} without tier stays valid by design.
    old = [{"key": "mapping", "value": "a → b"}]
    assert _node_options_summary_error(old, "p", plugin="field_mapper") is None
    new = [{"key": "mapping", "value": "a → b", "tier": "common"}]
    assert _node_options_summary_error(new, "p", plugin="field_mapper") is None
    bad = [{"key": "mapping", "value": "a → b", "tier": "loud"}]
    assert _node_options_summary_error(bad, "p", plugin="field_mapper") is not None
```

Any existing test in this file iterating `_NODE_OPTION_SUMMARY_ALLOWLIST` values as key tuples (grep `_NODE_OPTION_SUMMARY_ALLOWLIST` in the file) is updated to iterate `.keys()` of the inner mapping.

Create `tests/unit/web/catalog/test_guided_option_tier_parity.py` (lives in the catalog suite because it constructs the catalog service; uses the public accessor):

```python
"""The guided option-summary allowlist's tiers derive from the catalog lowering.

``protocol._NODE_OPTION_SUMMARY_ALLOWLIST`` carries a tier per allowlisted
key because the wire-stage and proposal projections — and the audit
verifier's re-derivation — run without a catalog handle. This pin keeps that
table honest: annotate an allowlisted knob with a ``composer_tier`` and the
table must follow (elspeth-ca456d9d8d).
"""

from __future__ import annotations

from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.catalog.service import CatalogServiceImpl
from elspeth.web.composer.guided.protocol import _NODE_OPTION_SUMMARY_ALLOWLIST


def test_allowlist_tiers_match_the_lowered_knob_schema() -> None:
    svc = CatalogServiceImpl(get_shared_plugin_manager())
    for plugin, tiers in _NODE_OPTION_SUMMARY_ALLOWLIST.items():
        # Only transforms are allowlisted today; the allowlist is keyed by plugin
        # name without a kind, so allowlisting a source/sink means extending this.
        fields = {field["name"]: field for field in svc.get_schema("transform", plugin).knob_schema["fields"]}
        for key, tier in tiers.items():
            assert key in fields, f"{plugin}.{key} is allowlisted but not a lowered knob"
            assert fields[key]["tier"] == tier, f"{plugin}.{key}: allowlist says {tier}, catalog lowers {fields[key]['tier']}"
```

Run: `pytest tests/unit/web/composer/guided/test_protocol.py tests/unit/web/catalog/test_guided_option_tier_parity.py -q -k "node_options_summary or allowlist_tiers"` → FAIL (`KeyError: 'tier'`; `.items()` on a tuple).

- [ ] **Step 2: Implement the backend and run the durable-row sweep**

`protocol.py` (`Literal`/`NotRequired` are already imported at `:12`; `FieldTier` may be imported from `elspeth.web.catalog.knob_schema` beside the existing `:16` import):

```python
# Server-owned allowlist of display-projected option keys, each with its
# presentational catalog tier (elspeth-ca456d9d8d). Per-plugin on purpose:
# adding a plugin or a key here is a deliberate per-option decision, and two
# plugins may tier a same-named knob differently. The tier lives here rather
# than being derived because both projections and the audit verifier's
# re-derivation run without a catalog handle; tests/unit/web/catalog/
# test_guided_option_tier_parity.py pins every entry to the lowering.
_NODE_OPTION_SUMMARY_ALLOWLIST: Mapping[str, Mapping[str, FieldTier]] = {
    "field_mapper": {"mapping": "common", "select_only": "common"},
}
_MAX_NODE_OPTION_SUMMARY_PAIRS = 20
_MAX_NODE_OPTION_SUMMARY_VALUE = 240


class _NodeOptionSummary(TypedDict):
    key: str
    value: str
    # Emitted on every fresh projection; NotRequired because durable turns
    # written before the tier landed replay through the same shape.
    tier: NotRequired[FieldTier]
```

`node_options_summary` (`:925-945`) — the invariant prose in its `@trust_boundary` is judge-visible and must describe the new return: extend it with "…each pair carrying the allowlist's presentational ``tier``…". Body:

```python
    tiers = _NODE_OPTION_SUMMARY_ALLOWLIST.get(plugin or "", {})
    if not tiers or not isinstance(options, Mapping):
        return []
    summary: list[_NodeOptionSummary] = []
    for key, tier in tiers.items():
        if key not in options:
            continue
        value = _NODE_OPTION_SUMMARY_RENDERERS[key](options[key])
        if value:
            summary.append({"key": key, "value": value, "tier": tier})
    return summary
```

(No unguarded index on a second table — the tier comes from the same iteration, so the `non_raising=True` invariant holds by construction.) `public_node_option_keys` (`:948-958`): replace the `return` line ONLY — `return frozenset(_NODE_OPTION_SUMMARY_ALLOWLIST.get(plugin or "", {}).keys())` — its callers want the key set only; the `if plugin is not None and type(plugin) is not str: raise TypeError(...)` guard above it (`:955`) is a boundary control and stays. `_node_options_summary_error` (`:960-985`): `allowed = _NODE_OPTION_SUMMARY_ALLOWLIST.get(plugin or "", {})` (membership on a mapping works unchanged), and the pair check becomes:

```python
        # A stored pair may LACK tier (pre-tier durable turns replay here) but
        # may never carry an unknown key or a non-tier value: the expected key
        # set follows the payload on purpose.
        expected_keys = (
            frozenset({"key", "value", "tier"})
            if isinstance(item, Mapping) and "tier" in item
            else frozenset({"key", "value"})
        )
        pair, error = _exact_nested_mapping(item, expected_keys, item_path)
        if error is not None:
            return error
        assert pair is not None
        if "tier" in pair and pair["tier"] not in ("essential", "common", "advanced"):
            return f"{item_path}.tier is not a composer field tier"
```

(A computed expected-keys frozenset handed to `_exact_nested_mapping` is existing house style — `protocol.py:1301-1304`.) `emitters.py:768` and `planning.py:3811` need no edit.

`knob_schema.py:61`: `tier: FieldTier`. Making `tier` a required key of the `total=True` TypedDict also requires seeding it where the literal is built — `_base_field` constructs `field: KnobField = {"name": …, "label": …, "kind": …, "required": …, "nullable": …}` (`:177-183`) and `_attach_tier` fills `tier` in afterwards (`:199`), which strict mypy (`pyproject.toml` `[tool.mypy] strict = true`) rejects as a missing key. Add `"tier": "common",` to that literal (`_attach_tier` overwrites it; the discriminator literal at `:577` already carries it). Run `pytest tests/unit/web/catalog -q` — goldens must NOT change — and `.venv/bin/mypy src/elspeth/web/catalog/knob_schema.py src/elspeth/web/composer/guided/protocol.py` → clean (mypy is also the pre-commit hook at `.pre-commit-config.yaml:74-76`, so this is what would otherwise fire at Step 3's commit).

Durable-row sweep (read-only; the gate for decision 2):

```bash
sqlite3 -readonly data/sessions.db .dump | grep -c node_options_summary
```

Expected: `0` (note `grep -c` prints `0` and exits 1 on zero matches — do not wrap this in `set -e`; the printed count is the result). Record the count and the DB mtime in the commit message. A non-zero count means a durable field_mapper projection exists: STOP and surface it — the options are DB recreation (pre-release posture) or a verifier-side normalisation, and that is the operator's call, not the lane's.

Equality pins on emitter output gain `"tier": "common"`: `test_emitters.py:661,:677` and `test_proposal_audit_projection.py:237` (its tamper case still expects `AuditIntegrityError`). Validator-input fixtures stay old-shape deliberately (`test_propose_pipeline_protocol.py:580-599`, `test_chat_solver.py:3882`) — they are the admit-optional proof. The full set of test files referencing `node_options_summary` (whole `tests/` tree, verified): `test_proposal_audit_projection.py`, `test_proposals.py`, `test_bind_reviewed_components.py`, `test_emitters.py`, `test_propose_pipeline_protocol.py`, `test_protocol.py`, `test_collector_guard.py`, `test_chat_solver.py`, `tests/unit/web/aws_ecs_acceptance/test_receipt_contracts.py`, `tests/unit/web/aws_ecs_acceptance/test_cleanup_control_service.py` — run all ten:

```bash
pytest tests/unit/web/composer/guided tests/unit/web/composer/test_proposals.py tests/integration/web/composer/guided/test_proposal_audit_projection.py tests/unit/web/aws_ecs_acceptance tests/unit/web/catalog -q
```

Expected: PASS.

Per-task lint-corpus diff (memo: mandatory for every backend-touching task): `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules all --root src/elspeth > /tmp/claude-1000/-home-john-elspeth/w2-lints-task5.txt; diff /tmp/claude-1000/-home-john-elspeth/w2-lints-before.txt /tmp/claude-1000/-home-john-elspeth/w2-lints-task5.txt` → no added findings. Task 9's whole-wave diff remains the backstop.

- [ ] **Step 3: Commit the backend half**

```bash
git add src/elspeth/web/composer/guided/protocol.py src/elspeth/web/catalog/knob_schema.py tests/unit/web/composer/guided/test_protocol.py tests/unit/web/catalog/test_guided_option_tier_parity.py tests/unit/web/composer/guided/test_emitters.py tests/integration/web/composer/guided/test_proposal_audit_projection.py
git commit -m "feat(guided): node_options_summary carries the allowlist's presentational tier per pair, emit-always admit-optional; KnobField.tier required (elspeth-ca456d9d8d)"
```

- [ ] **Step 4: Frontend wire type + decoder**

`types/guided.ts:718-721`:

```ts
export interface NodeOptionSummary {
  key: string;
  value: string;
  /** Presentational catalog tier; absent on pre-tier durable payloads → "common". */
  tier?: FieldTier;
}
```

(`FieldTier` is declared later in the same file at `:507`.) Create `guided/optionTiers.ts`:

```ts
import type { FieldTier, NodeOptionSummary } from "@/types/guided";

/** Absent tier = "common": pre-tier durable payloads (elspeth-ca456d9d8d). */
export function optionTier(entry: NodeOptionSummary): FieldTier {
  return entry.tier ?? "common";
}
```

`guidedDecoder.ts:198-207` — admit the optional key (`exactRecord(value, path, required, optional)`, `:113-118`):

```ts
function nodeOptionsSummary(value: unknown, path: string): NodeOptionSummary[] {
  return arrayValue(value, path).map((item, index) => {
    const entryPath = `${path}[${index}]`;
    const entry = exactRecord(item, entryPath, ["key", "value"], ["tier"]);
    const tier = entry.tier === undefined ? undefined : stringValue(entry.tier, `${entryPath}.tier`);
    if (tier !== undefined && tier !== "essential" && tier !== "common" && tier !== "advanced") {
      invalid(`${entryPath}.tier`, "expected a composer field tier");
    }
    return {
      key: stringValue(entry.key, `${entryPath}.key`),
      value: stringValue(entry.value, `${entryPath}.value`),
      ...(tier !== undefined ? { tier: tier as NodeOptionSummary["tier"] } : {}),
    };
  });
}
```

Add decoder tests in `guidedDecoder.test.ts` beside the existing node-option cases: old shape decodes (no tier), new shape decodes, `tier: "loud"` throws.

- [ ] **Step 5: Failing WireStageTurn tests**

`WireStageTurn.test.tsx` builds `WireStageData` through `canonicalData(overrides)` (`:13-70`): source `inline_blob`, node `field_mapper` (`node_options_summary: []`), output `json`. Append (imports: `usePreferencesStore`, `resetStore`, `act`, `stepLabelForPlugin` from `@/components/chat/interpretationStepLabel`, `expectNoIdentifiersInDefaultDom`):

```tsx
describe("detail level (elspeth-ca456d9d8d)", () => {
  beforeEach(() => resetStore(usePreferencesStore));

  const withNodeOptions = (node_options_summary: NodeOptionSummary[]): WireStageData => {
    const base = canonicalData();
    return { ...base, nodes: [{ ...base.nodes[0], node_options_summary }] };
  };

  it("keeps cardinality enums, field lists, and raw plugin ids out of the default row; shows display names", () => {
    const { container } = render(<WireStageTurn data={canonicalData()} />);
    const components = screen.getByRole("region", { name: "Reviewed components" });
    // Positive: the display name renders (field_mapper → "Output", inline_blob/json likewise).
    expect(within(components).getByText(`(${stepLabelForPlugin("field_mapper")})`)).toBeInTheDocument();
    expect(within(components).getByText(`(${stepLabelForPlugin("inline_blob")})`)).toBeInTheDocument();
    // Negatives, on a fixture that DOES carry these values by construction.
    expect(components.textContent).not.toMatch(/\(field_mapper\)|\(inline_blob\)|\(json\)/);
    expect(components.textContent).not.toMatch(/zero or many|Cardinality:|Required fields:|Guaranteed fields:/);
    expect(screen.getByText(/Validation failure:/)).toBeInTheDocument();
    // Scoped to the per-row class: the routes-level raw-edge dump
    // (`.wire-stage__raw`, WireStageTurn.tsx:536-541) also has a
    // "Technical details" summary, is not flag-controlled, and is untouched.
    const rowDetails = container.querySelectorAll("details.wire-stage__row-technical");
    expect(rowDetails).toHaveLength(3); // exactly one per source / node / output row of canonicalData()
    for (const details of rowDetails) {
      expect(details).not.toHaveAttribute("open");
    }
    expectNoIdentifiersInDefaultDom(container, {
      allowSelectors: [".wire-stage__row-technical", ".wire-stage__raw"],
    });
  });

  it("opens every per-row Technical details when show_advanced flips on a mounted turn", () => {
    const { container } = render(<WireStageTurn data={canonicalData()} />);
    act(() => usePreferencesStore.setState({ showAdvanced: true }));
    const rowDetails = container.querySelectorAll("details.wire-stage__row-technical");
    expect(rowDetails).toHaveLength(3);
    for (const details of rowDetails) {
      expect(details).toHaveAttribute("open");
    }
    // The routes-level raw dump is deliberately NOT flag-controlled.
    expect(container.querySelector("details.wire-stage__raw")).not.toHaveAttribute("open");
  });

  it("shows common option pairs inline and advanced pairs only in the disclosure", () => {
    render(
      <WireStageTurn
        data={withNodeOptions([
          { key: "mapping", value: "a → b", tier: "common" },
          { key: "select_only", value: "only the mapped fields are kept", tier: "advanced" },
        ])}
      />,
    );
    expect(screen.getByText("Mapping: a → b").closest("details")).toBeNull();
    expect(
      screen.getByText("Select only: only the mapped fields are kept").closest("details"),
    ).not.toBeNull();
  });

  it("treats a tier-less pair as common (pre-tier durable payloads)", () => {
    render(<WireStageTurn data={withNodeOptions([{ key: "mapping", value: "a → b" }])} />);
    expect(screen.getByText("Mapping: a → b").closest("details")).toBeNull();
  });
});
```

(The per-row count is exactly 3 for `canonicalData()` — one source, one node, one output — by construction of the class-scoped query; the routes-level raw dump keeps its own unscoped "Technical details" summary and is asserted closed.)

- [ ] **Step 6: Implement the guided surfaces**

1. Create `guided/behaviorSummary.ts`: move `gateSummary` (`ProposePipelineTurn.tsx:123-157`) and `behaviorSummary` (`:159-196`) verbatim, exported; `ProposePipelineTurn.tsx` imports them (no behaviour change).
2. `WireStageTurn.tsx` — `const showAdvanced = useShowAdvanced();`; import `behaviorSummary` from `./behaviorSummary` and `optionTier` from `./optionTiers`. Node rows (`:442-477`) become:

```tsx
                <li key={node.stable_id}>
                  <strong>{node.label}</strong>{" "}
                  <span>({stepLabelForPlugin(node.plugin ?? node.node_type)})</span>
                  <p>
                    {node.behavior.kind === "gate"
                      ? `When ${node.behavior.condition}`
                      : behaviorSummary(node.behavior, (stableId) =>
                          data.nodes.find((c) => c.stable_id === stableId)?.label ?? null)}
                  </p>
                  {node.node_options_summary
                    .filter((entry) => optionTier(entry) !== "advanced")
                    .map((entry) => (
                      <p key={entry.key}>{nodeOptionText(entry)}</p>
                    ))}
                  <details className="wire-stage__row-technical" open={showAdvanced}>
                    <summary>Technical details</summary>
                    <p>{cardinalityText(node.row_cardinality)}</p>
                    <p>{fieldsText("Required", node.required_fields)}</p>
                    <p>{fieldsText("Guaranteed", node.guaranteed_fields)}</p>
                    {behaviorDetails(node.behavior, routeDestinationFor(node.stable_id),
                      (stableId) => data.nodes.find((c) => c.stable_id === stableId)?.label ?? null,
                    ).map((detail) => <p key={detail}>{detail}</p>)}
                    {node.node_options_summary
                      .filter((entry) => optionTier(entry) === "advanced")
                      .map((entry) => <p key={entry.key}>{nodeOptionText(entry)}</p>)}
                    {node.structured_output_fields.length > 0 ? (
                      <ul aria-label={`${node.label} structured output fields`}>
                        {node.structured_output_fields.map((field) => (
                          <li key={`${field.query}:${field.field}`}>
                            {`${field.field} (${field.type}) from ${field.query}${
                              field.enum_values.length > 0 ? `; values: ${field.enum_values.join(", ")}` : ""
                            }`}
                          </li>
                        ))}
                      </ul>
                    ) : null}
                    <p>Stable ID: <code>{node.stable_id}</code></p>
                  </details>
                </li>
```

Source rows (`:425-440`) keep `label` + `({stepLabelForPlugin(source.plugin)})` + the `Validation failure:` sentence visible; cardinality, guaranteed fields, and `Stable ID: <code>…</code>` move into the same per-row `<details className="wire-stage__row-technical" open={showAdvanced}>`. Output rows (`:479-507`) keep `label` + `({stepLabelForPlugin(output.plugin)})` + `Write failure:` visible; field lists, `Schema mode:` + the business-schema `<ul>`, and the Stable ID move in. The old `<details><summary>Stable ID</summary>…` disclosures are absorbed. The Routes section, its roll-up, the routes-level raw-edge `Technical details` (`:536-541`), warnings, and the blocker panel (`:310`, `blockersId`) are untouched.
3. `ProposePipelineTurn.tsx` — graph subtitles (`:277`, `:283`, `:289`): `stepLabelForPlugin(source.plugin.id)`, `node.plugin === null ? null : stepLabelForPlugin(node.plugin.id)`, `stepLabelForPlugin(output.plugin.id)` (import it); the components list (`:407-433`) replaces ` · ${node.plugin.id}` with ` · ${stepLabelForPlugin(node.plugin.id)}` (same for source/output lines) and gates its `node_options_summary` loop: common pairs always, advanced pairs only when `useShowAdvanced()` is true (this list has no per-row disclosure; a plain gate — no new surface — is the honest form of "debug mode expands"). `ProposePipelineTurn.test.tsx` gains a top-level `beforeEach(() => resetStore(usePreferencesStore))` (the component is now a flag reader) and a test that an advanced pair renders only with the flag.
   **Live-check addendum (39578c6f Check 1, flag off):** the Spec tab's `Plugin` row (`PipelineSpecView.tsx:104-107`) and the node inspector's plugin line (`GraphView.tsx:697-698`) render the raw plugin id (`field_mapper`, `llm`, …) as plain `<dd>`/`<p>` text — the same "humanise plugin.id" defect this ticket owns, on two more surfaces. Fix both identically: `<dd title={row.plugin}>{pluginDisplayName(row.plugin)}</dd>` and `<p className="graph-config-plugin" title={config.plugin}>{pluginDisplayName(config.plugin)}</p>` (`pluginDisplayName` from `@/components/catalog/pluginDisplayName` — the catalog register, e.g. "Field Mapper", since these rows name the plugin's identity; `stepLabelForPlugin` stays the guided-turn register). Neither test file pins these values today (grepped); add one assertion each — `PipelineSpecView.test.tsx`: the `Plugin` `<dd>` reads `pluginDisplayName("csv")` with `title="csv"`; `GraphView.test.tsx`: the `.graph-config-plugin` line likewise. Do not touch `routingValue` (`PipelineSpecView.tsx:52-66`): 3b7281965 renders `branches` maps as prose like `routes`, and `PipelineSpecView.test.tsx` pins it. Task 8 edits both files afterwards (it passes `plugin` to `OptionRows`); the two tasks are sequential, so there is no conflict.
4. `guided.css` — after `.wire-stage__raw`:

```css
.wire-stage__row-technical {
  margin-top: 0.35rem;
}
.wire-stage__row-technical > summary {
  cursor: pointer;
  color: var(--color-text-secondary);
  font-size: 0.86rem;
}
```

5. `SingleSelectTurn.tsx:39-60` + `types/guided.ts:224-228` + `ChatPanel.tsx` — uploaded-time disambiguator. `GuidedSourceBlobCandidate` gains `createdAt: string;`; both construction sites in `ChatPanel.tsx` (`:1261-1265` and the `readyGuidedSourceBlobs` map `:1223-1245`) add `createdAt: blob.created_at` (`BlobMetadata.created_at`, `types/index.ts:1295` — client-side only, no wire change). `sourceBlobCandidateLabel` prefers the time:

```ts
const UPLOAD_TIME_FORMATTER = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });

function sourceBlobCandidateLabel(
  candidate: GuidedSourceBlobCandidate,
  candidates: readonly GuidedSourceBlobCandidate[],
): string {
  const duplicates = candidates.filter((item) => item.filename === candidate.filename);
  if (duplicates.length === 1) return candidate.filename;
  const stamps = duplicates.map((item) => new Date(item.createdAt));
  const distinct = new Set(stamps.map((d) => UPLOAD_TIME_FORMATTER.format(d)));
  if (stamps.every((d) => !Number.isNaN(d.getTime())) && distinct.size === duplicates.length) {
    return `${candidate.filename} (uploaded ${UPLOAD_TIME_FORMATTER.format(new Date(candidate.createdAt))})`;
  }
  return `${candidate.filename} — ${formatCandidateBytes(candidate.sizeBytes)} — ID ${distinguishingBlobIdSuffix(candidate, duplicates)}`;
}
```

Update `SingleSelectTurn.test.tsx` fixtures with `createdAt` and add a same-minute-collision case pinning the ID-fragment fallback.
6. `SchemaFormTurn.test.tsx` — absorb the two deferred Wave 1 minors (no component change; reuse the Wave 1 block's `pluginPayload`/`field` helpers at `:9`/`:18`, add `within`):

```tsx
  it("renders a schema whose every field is advanced entirely inside the disclosure", async () => {
    const user = userEvent.setup();
    const allAdvanced = pluginPayload([
      field({ name: "temperature", label: "Temperature", kind: "number-float", tier: "advanced", default: 0 }),
    ]);
    render(<SchemaFormTurn payload={allAdvanced} onSubmit={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Edit" }));
    const details = screen.getByText("Advanced settings (1)").closest("details") as HTMLElement;
    expect(within(details).getByRole("spinbutton", { name: "Temperature" })).toBeInTheDocument();
    expect(screen.getByRole("spinbutton", { name: "Temperature" }).closest("details")).toBe(details);
  });
```

- [ ] **Step 7: Run the guided suites and the tutorial canary**

Run: `npx vitest run src/components/chat/guided src/components/tutorial src/api/guidedDecoder.test.ts src/api/guidedDecoder.gate.test.ts`
Expected: PASS — pre-existing WireStageTurn/ProposePipelineTurn tests pinning the old inline cardinality/fields/plugin-id text move to DOM-position assertions (the strings still exist inside `<details>`; do not delete pins). The tutorial suite proves ADR-031.

- [ ] **Step 8: Commit the frontend half**

```bash
git add src/elspeth/web/frontend/src/types/guided.ts src/elspeth/web/frontend/src/api/guidedDecoder.ts src/elspeth/web/frontend/src/api/guidedDecoder.test.ts src/elspeth/web/frontend/src/components/chat/guided/behaviorSummary.ts src/elspeth/web/frontend/src/components/chat/guided/optionTiers.ts src/elspeth/web/frontend/src/components/chat/guided/WireStageTurn.tsx src/elspeth/web/frontend/src/components/chat/guided/WireStageTurn.test.tsx src/elspeth/web/frontend/src/components/chat/guided/ProposePipelineTurn.tsx src/elspeth/web/frontend/src/components/chat/guided/ProposePipelineTurn.test.tsx src/elspeth/web/frontend/src/components/chat/guided/SingleSelectTurn.tsx src/elspeth/web/frontend/src/components/chat/guided/SingleSelectTurn.test.tsx src/elspeth/web/frontend/src/components/chat/guided/SchemaFormTurn.test.tsx src/elspeth/web/frontend/src/components/chat/ChatPanel.tsx src/elspeth/web/frontend/src/components/chat/guided/guided.css src/elspeth/web/frontend/src/components/workspace/PipelineSpecView.tsx src/elspeth/web/frontend/src/components/workspace/PipelineSpecView.test.tsx src/elspeth/web/frontend/src/components/inspector/GraphView.tsx src/elspeth/web/frontend/src/components/inspector/GraphView.test.tsx
git commit -m "feat(guided): per-row Technical details for wire-stage internals; tiered option pairs; plugin display names on guided turns, Spec tab and inspector; uploaded-time blob disambiguator (elspeth-ca456d9d8d)"
```

**Ticket disposition:** CLOSE at end of wave.

---

### Task 6: Plugin catalog — chips, characteristic strip, Schema view (`elspeth-8555a6a9e0`, roadmap row 6)

**Files:**
- Modify: `src/elspeth/web/frontend/src/components/catalog/auditCharacteristics.ts` (new exported constant)
- Modify: `src/elspeth/web/frontend/src/components/catalog/FilterChipStrip.tsx` (`:70-98`)
- Modify: `src/elspeth/web/frontend/src/components/catalog/PluginCard.tsx` (strip `:182-196`, `expanded` state `:135`, Schema button `:220-229`, `renderFields` `:102-112`)
- Modify: `src/elspeth/web/frontend/src/components/catalog/catalog.css` (`:351-374` field rules)
- Test: `FilterChipStrip.test.tsx`, `PluginCard.test.tsx`

**Interfaces:**
- Consumes: `useShowAdvanced()`.
- Produces: `export const DEFAULT_VISIBLE_AUDIT_FLAGS: readonly AuditCharacteristicFlag[] = ["quarantine", "credentials", "external_call"];` — the three flags that change what happens to the reader's data (labels "quarantines bad rows", "needs credentials", "network call"), shared by strip and card.
- `AuditCharacteristicIcon` renders `<span title={tooltip}><span className="audit-icon-label">{label}</span></span>` — no `role`; tests query by label text.
- Unchanged: kind tabs, search, "Details" (Use when / Avoid when / Example), trust-tier exclusion (`FilterChipStrip.tsx:18-19`), `AuditCharacteristicIcon` tooltips.

- [ ] **Step 1: Failing tests**

`FilterChipStrip.test.tsx` (add `usePreferencesStore`, `resetStore`; top-level `beforeEach(() => resetStore(usePreferencesStore))`):

```tsx
describe("detail level (elspeth-8555a6a9e0)", () => {
  const audits = ["quarantine", "credentials", "external_call", "coerce", "non_deterministic"];
  const empty = { capabilityTags: new Set<string>(), auditCharacteristics: new Set<string>() };

  it("hides the Capability group and non-behavioural audit chips by default", () => {
    render(<FilterChipStrip availableCapabilityTags={["csv", "llm"]} availableAuditCharacteristics={audits} filters={empty} onChange={vi.fn()} />);
    expect(screen.queryByText("Capability:")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "quarantines bad rows" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "needs credentials" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "network call" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "can coerce types" })).not.toBeInTheDocument();
  });

  it("shows everything with show_advanced on", () => {
    usePreferencesStore.setState({ showAdvanced: true });
    render(<FilterChipStrip availableCapabilityTags={["csv", "llm"]} availableAuditCharacteristics={audits} filters={empty} onChange={vi.fn()} />);
    expect(screen.getByText("Capability:")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "can coerce types" })).toBeInTheDocument();
  });
});
```

`PluginCard.test.tsx` (same imports + top-level reset; the fixture is the file's existing `PluginSummary` literal with `audit_characteristics` set to exactly `["deterministic", "quarantine", "credentials", "coerce"]`, and its `PluginSchemaInfo` literal with `json_schema: { properties: { profile: { type: "string" } }, required: ["profile"] }` — write these two literals into the test as `CARD_PLUGIN` / `CARD_SCHEMA` if the file's fixtures differ):

```tsx
describe("detail level (elspeth-8555a6a9e0)", () => {
  it("shows only the behavioural flags and no Schema button by default", () => {
    render(<PluginCard plugin={CARD_PLUGIN} schema={null} onExpand={vi.fn()} />);
    const strip = screen.getByRole("group", { name: "Audit characteristics" });
    expect(within(strip).getByText("quarantines bad rows")).toBeInTheDocument();
    expect(within(strip).getByText("needs credentials")).toBeInTheDocument();
    expect(within(strip).queryByText("deterministic")).not.toBeInTheDocument();
    expect(within(strip).queryByText("can coerce types")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Schema for / })).not.toBeInTheDocument();
  });

  it("shows all flags, the Schema button, and separated schema columns with show_advanced on", () => {
    usePreferencesStore.setState({ showAdvanced: true });
    render(<PluginCard plugin={CARD_PLUGIN} schema={CARD_SCHEMA} onExpand={vi.fn()} initialExpanded />);
    const strip = screen.getByRole("group", { name: "Audit characteristics" });
    for (const label of ["deterministic", "quarantines bad rows", "needs credentials", "can coerce types"]) {
      expect(within(strip).getByText(label)).toBeInTheDocument();
    }
    expect(screen.getByRole("button", { name: /^Schema for / })).toBeInTheDocument();
    const row = screen.getByText("profile").closest(".plugin-card-field-row") as HTMLElement;
    expect(within(row).getByText("string")).toBeInTheDocument();
    expect(within(row).getByText("required")).toBeInTheDocument();
  });

  it("closes an open Schema panel when the flag goes off on a mounted card", () => {
    usePreferencesStore.setState({ showAdvanced: true });
    render(<PluginCard plugin={CARD_PLUGIN} schema={CARD_SCHEMA} onExpand={vi.fn()} initialExpanded />);
    expect(screen.getByText("profile")).toBeInTheDocument();
    act(() => usePreferencesStore.setState({ showAdvanced: false }));
    expect(screen.queryByText("profile")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Schema for / })).not.toBeInTheDocument();
  });
});
```

Run: `npx vitest run src/components/catalog -t "detail level"` → FAIL.

- [ ] **Step 2: Implement**

`auditCharacteristics.ts` — add near the vocabulary:

```ts
/** The three flags that change what happens to the reader's data — the
 *  default-visible subset (elspeth-8555a6a9e0); the other nine render only
 *  with show_advanced. Shared by PluginCard's strip and FilterChipStrip so
 *  the two splits cannot drift. */
export const DEFAULT_VISIBLE_AUDIT_FLAGS: readonly AuditCharacteristicFlag[] = [
  "quarantine",
  "credentials",
  "external_call",
];
```

`FilterChipStrip.tsx`: `const showAdvanced = useShowAdvanced();`; wrap the Capability `ChipGroup` in `showAdvanced && (...)`; the Audit group maps over `availableAuditCharacteristics.filter((flag) => showAdvanced || (DEFAULT_VISIBLE_AUDIT_FLAGS as readonly string[]).includes(flag))`. An active filter in a hidden group still counts toward `anyActive`, so Clear filters remains reachable after a flag flip — leave `anyActive` as is.

`PluginCard.tsx`: `const showAdvanced = useShowAdvanced();`. Strip: map over `[...plugin.audit_characteristics].filter((flag) => showAdvanced || (DEFAULT_VISIBLE_AUDIT_FLAGS as readonly string[]).includes(flag)).sort()` and render the `role="group"` div only when that list is non-empty. Schema: derive one condition and use it for BOTH the trigger and the panel so they cannot separate —

```ts
  const schemaOpen = expanded && showAdvanced;
```

the `<Button … aria-expanded={schemaOpen}>Schema</Button>` (`:220-229`) renders inside `showAdvanced && (...)`, and the panel (`{expanded && (<div id={schemaPanelId} …>`) becomes `{schemaOpen && (…)}`. (`expanded` is independent component state, `:135`; a user who opened the panel and then turned the flag off must see it close.) `renderFields` (`:102-112`) gains a row class: `<div key={name} className="plugin-card-field-row">`.

`catalog.css` — beside the field rules at `:351-374`:

```css
.plugin-card-field-row {
  display: grid;
  grid-template-columns: minmax(8rem, max-content) minmax(4rem, max-content) auto;
  column-gap: 0.75rem;
  align-items: baseline;
}
.plugin-card-field-row .plugin-card-field-desc {
  grid-column: 1 / -1;
}
```

- [ ] **Step 3: Run**

Run: `npx vitest run src/components/catalog src/test/a11y`
Expected: pre-existing FilterChipStrip tests (`:28-98`) render capability chips — set the flag on in those; a11y suite covers the card. All PASS.

- [ ] **Step 4: Commit**

```bash
git add src/elspeth/web/frontend/src/components/catalog/auditCharacteristics.ts src/elspeth/web/frontend/src/components/catalog/FilterChipStrip.tsx src/elspeth/web/frontend/src/components/catalog/FilterChipStrip.test.tsx src/elspeth/web/frontend/src/components/catalog/PluginCard.tsx src/elspeth/web/frontend/src/components/catalog/PluginCard.test.tsx src/elspeth/web/frontend/src/components/catalog/catalog.css
git commit -m "feat(catalog): behavioural-flags-only strip and chips by default; Schema view and capability chips behind show_advanced (elspeth-8555a6a9e0)"
```

**Ticket disposition:** CLOSE at end of wave.

---

### Task 7: Version history grouping — a tree, every version still revertable (`elspeth-c8a402a9a4`, roadmap row 7 — depends on Task 1's sentence labels)

**Design decisions (flagged to the operator in the drafting report):**

1. **Widget role: `role="tree"` / `role="treeitem"`, replacing the listbox.** A collapsible group cannot be an `option` (`aria-expanded` is unsupported on `option`; a permanently unselectable option whose Enter changes the number of options is a non-value in a single-select listbox). `treeitem` supports `aria-expanded` and `aria-selected` natively, keeps the roving `aria-activedescendant` model, and keeps the Revert button unchanged. With `show_advanced` on (or when no group forms) the tree is flat — every item a leaf — which is conformant. Cost: every `option`/`listbox` query in `HeaderVersionSelector.test.tsx` (16 sites) is renamed; the e2e trigger name (`composer-workspace-geometry.spec.ts:409`) is unaffected. Cost if wrong: none to data — this is presentation; the fallback is to flatten by dropping grouping, which the helper's `showAdvanced` branch already implements.
2. **Selection is keyed by version NUMBER, never by list index.** Expanding or collapsing a group changes the row count; an index-addressed selection silently re-targets Revert (the reviewer's transcript: select v14, expand v15–v18, "Revert to v14" becomes "Revert to v18"). Revert is a destructive audit-visible action; the target is the version the user chose, full stop. A test pins it.
3. **Grouping keys on a structural discriminant, not the copy.** `versionLabels.ts` exports `versionLabelKind(version, all, messages): "applied" | "revert" | "seed" | "edited"`; `deriveVersionLabel` derives its copy from it and `buildVersionRows` groups on `kind === "edited"`. Wave 3's register batch (`elspeth-d74ab492dd`) can then rewrite the word "Edited" without turning grouping off.

**Files:**
- Modify: `src/elspeth/web/frontend/src/components/header/versionLabels.ts` (`deriveVersionLabel` `:230-253`), `versionLabels.test.ts`
- Create: `src/elspeth/web/frontend/src/components/header/versionGrouping.ts` + `versionGrouping.test.ts`
- Modify: `src/elspeth/web/frontend/src/components/header/HeaderVersionSelector.tsx` (`:54-80` row derivation, `:141-145` scroll-into-view effect, `:146-150` reset effect, `:166-208` keyboard, `:212-214` selection, `:225` trigger `aria-haspopup`, `:244-330` list), `header.css`
- Modify: `src/elspeth/web/frontend/src/test/composerFixtures.ts` (version-history fixture)
- Test: `HeaderVersionSelector.test.tsx`

**Interfaces:**
- Consumes: `deriveVersionLabel` (Task 1 sentence), `versionOperationIdentifier` (Task 1), `useShowAdvanced()`.
- Produces:

```ts
// versionLabels.ts
export type VersionLabelKind = "applied" | "revert" | "seed" | "edited";
export function versionLabelKind(version, allVersions, messages): VersionLabelKind;

// versionGrouping.ts
export interface VersionRow { kind: "version"; version: CompositionStateVersion; }
export interface GroupRow { kind: "group"; id: string; versions: CompositionStateVersion[]; expanded: boolean; }
export type VersionListRow = VersionRow | GroupRow;
export function buildVersionRows(
  displayVersions: CompositionStateVersion[],   // current first, then descending
  kindFor: (v: CompositionStateVersion) => VersionLabelKind | "snapshot",
  currentVersion: number | null,
  showAdvanced: boolean,
  expandedGroupIds: ReadonlySet<string>,
): VersionListRow[];
```

- Out of scope (ticket §3): edit-source labels need a backend `source` field on `CompositionStateVersion` (`types/index.ts:270-281` has none); the closing comment offers the backend ticket.
- Unchanged: Revert button + confirm (`:330-361`), snapshot-only filtering, version-number gap honesty.

- [ ] **Step 1: The discriminant**

`versionLabels.ts` — above `deriveVersionLabel`:

```ts
export type VersionLabelKind = "applied" | "revert" | "seed" | "edited";

/** The structural fact deriveVersionLabel's copy stands for. Grouping in the
 *  history tree keys on this, never on the visible string. */
export function versionLabelKind(
  version: CompositionStateVersion,
  allVersions: CompositionStateVersion[],
  messages: ChatMessage[],
): VersionLabelKind {
  if (appliedToolCallName(version, messages) !== null) return "applied";
  const derivedFrom = version.derived_from_state_id;
  if (derivedFrom) {
    const target = allVersions.find((candidate) => candidate.id === derivedFrom);
    if (target && target.version < version.version - 1) return "revert";
  }
  return version.version === 1 ? "seed" : "edited";
}
```

and `deriveVersionLabel` becomes a switch over `versionLabelKind(...)` producing the same strings it produces today (`Applied: <sentence>` / `Reverted to v${target.version}` / `Session created` / `Edited`), so the existing `deriveVersionLabel` tests are unchanged; add one test per kind for `versionLabelKind` in `versionLabels.test.ts` (reuse the fixtures at `:164-236`).

- [ ] **Step 2: Failing grouping-helper test**

`versionGrouping.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import type { CompositionStateVersion } from "@/types/index";
import { buildVersionRows } from "./versionGrouping";

function v(version: number): CompositionStateVersion {
  return { id: `id-${version}`, version, created_at: "2026-08-29T10:00:00Z", node_count: 11 };
}
const versions = [19, 18, 17, 16, 15, 14, 13, 12, 11].map(v);
const kindFor = (row: CompositionStateVersion) => (row.version === 14 ? "applied" : "edited") as const;

describe("buildVersionRows", () => {
  it("groups consecutive edited runs, keeps current and applied rows standalone", () => {
    const rows = buildVersionRows(versions, kindFor, 19, false, new Set());
    expect(rows.map((row) => (row.kind === "group" ? row.id : `v${row.version.version}`))).toEqual([
      "v19", "v15-v18", "v14", "v11-v13",
    ]);
  });

  it("marks a group expanded without changing the row list (members render nested)", () => {
    const rows = buildVersionRows(versions, kindFor, 19, false, new Set(["v15-v18"]));
    const group = rows[1];
    expect(group.kind).toBe("group");
    expect(group.kind === "group" && group.expanded).toBe(true);
    expect(group.kind === "group" && group.versions.map((m) => m.version)).toEqual([18, 17, 16, 15]);
  });

  it("never groups a single edited row, and show_advanced renders everything flat", () => {
    expect(buildVersionRows([v(19), v(18)], () => "edited", 19, false, new Set()).every((r) => r.kind === "version")).toBe(true);
    const flat = buildVersionRows(versions, kindFor, 19, true, new Set());
    expect(flat.every((row) => row.kind === "version")).toBe(true);
    expect(flat).toHaveLength(9);
  });

  it("keeps every version reachable through its group (revert safety, epic rule 1)", () => {
    const rows = buildVersionRows(versions, kindFor, 19, false, new Set());
    const reachable = rows.flatMap((row) => (row.kind === "group" ? row.versions : [row.version]));
    expect(reachable.map((m) => m.version).sort((a, b) => a - b)).toEqual([11, 12, 13, 14, 15, 16, 17, 18, 19]);
  });
});
```

Run: `npx vitest run src/components/header/versionGrouping.test.ts` → module not found.

- [ ] **Step 3: Implement the helper**

`versionGrouping.ts`:

```ts
// ============================================================================
// versionGrouping — collapses runs of indistinguishable edited versions in
// the header history tree (elspeth-c8a402a9a4). Grouping is presentation
// only: every member stays in its group's `versions`, so every version a
// standard user could revert to remains reachable (epic rule 1 — Revert is an
// ACTION; grouping must never remove a revert target). Applied versions,
// reverts, the v1 seed, snapshot-only rows, and the current version always
// stand alone: their labels carry information a reader acts on. Grouping keys
// on versionLabelKind, never on the visible copy.
// ============================================================================

import type { CompositionStateVersion } from "@/types/index";
import type { VersionLabelKind } from "./versionLabels";

export interface VersionRow { kind: "version"; version: CompositionStateVersion; }
export interface GroupRow {
  kind: "group";
  id: string;
  versions: CompositionStateVersion[];
  expanded: boolean;
}
export type VersionListRow = VersionRow | GroupRow;

export function groupId(members: CompositionStateVersion[]): string {
  const numbers = members.map((m) => m.version);
  return `v${Math.min(...numbers)}-v${Math.max(...numbers)}`;
}

export function buildVersionRows(
  displayVersions: CompositionStateVersion[],
  kindFor: (v: CompositionStateVersion) => VersionLabelKind | "snapshot",
  currentVersion: number | null,
  showAdvanced: boolean,
  expandedGroupIds: ReadonlySet<string>,
): VersionListRow[] {
  if (showAdvanced) {
    return displayVersions.map((version) => ({ kind: "version", version }));
  }
  const rows: VersionListRow[] = [];
  let run: CompositionStateVersion[] = [];
  const flushRun = (): void => {
    if (run.length >= 2) {
      const id = groupId(run);
      rows.push({ kind: "group", id, versions: run, expanded: expandedGroupIds.has(id) });
    } else {
      for (const version of run) rows.push({ kind: "version", version });
    }
    run = [];
  };
  for (const version of displayVersions) {
    const groupable = version.version !== currentVersion && kindFor(version) === "edited";
    if (groupable) {
      run.push(version);
    } else {
      flushRun();
      rows.push({ kind: "version", version });
    }
  }
  flushRun();
  return rows;
}
```

Run the helper test → PASS.

- [ ] **Step 4: Failing component tests**

Add to `composerFixtures.ts`:

```ts
/** Nine bare version rows shaped like the live v19 session (4 edited, one
 *  applied at v14, 3 edited below it) — no content payloads, so isSnapshotOnly
 *  honestly declines to hide any of them (versionLabels.ts contract). */
export function makeVersionHistory(): CompositionStateVersion[] {
  return [19, 18, 17, 16, 15, 14, 13, 12, 11].map((version) => ({
    id: `state-${version}`,
    version,
    created_at: `2026-08-29T0${version - 10}:00:00Z`,
    node_count: version >= 15 ? 11 : 5,
  }));
}
```

`HeaderVersionSelector.test.tsx`: (a) add a top-level `beforeEach(() => resetStore(usePreferencesStore))` beside the existing store setup at `:54-70` (the component becomes a flag reader); (b) rename every `getByRole("listbox"…)` → `"tree"` and `getByRole("option"…)` / `queryByRole("option"…)` → `"treeitem"` (16 sites: `:114,132,148,164,196,223,228,231,256,272,295,319,343,348,359,363`); (c) append (the applied v14 needs a message with a settled `set_pipeline` tool call stamped `applied_state_version: 14` — copy the message literal shape from `versionLabels.test.ts:67-124` into a local `appliedSetPipelineMessage(14)`):

```tsx
describe("grouping (elspeth-c8a402a9a4)", () => {
  beforeEach(() => {
    useSessionStore.setState({
      compositionState: { version: 19, sources: {}, nodes: [], edges: [], outputs: [] } as never,
      stateVersions: makeVersionHistory(),
      messages: [appliedSetPipelineMessage(14)],
    } as never);
  });

  it("renders 4 top-level items by default and every version via expansion", async () => {
    const user = userEvent.setup();
    render(<HeaderVersionSelector />);
    await user.click(screen.getByRole("button", { name: /Composition history/ }));
    // v19 (current), v15–v18 group, v14 (applied), v11–v13 group — from the helper's unit test.
    expect(screen.getAllByRole("treeitem")).toHaveLength(4);
    const group = screen.getByRole("treeitem", { name: /Versions 15 to 18 — 4 edits/ });
    expect(group).toHaveAttribute("aria-expanded", "false");
    await user.click(group);
    expect(group).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("treeitem", { name: /^Version 17 / })).toBeInTheDocument();
    expect(screen.getAllByRole("treeitem")).toHaveLength(8); // 4 top-level + 4 members
  });

  it("keeps the Revert target on the version the user selected across a group expansion", async () => {
    const user = userEvent.setup();
    const revertToVersion = vi.fn();
    useSessionStore.setState({ revertToVersion } as never);
    render(<HeaderVersionSelector />);
    await user.click(screen.getByRole("button", { name: /Composition history/ }));
    await user.click(screen.getByRole("treeitem", { name: /^Version 14 / }));
    expect(screen.getByRole("button", { name: "Revert to version 14" })).toBeInTheDocument();
    await user.click(screen.getByRole("treeitem", { name: /Versions 15 to 18/ }));
    // Rows changed under the selection; the target did not.
    expect(screen.getByRole("button", { name: "Revert to version 14" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Revert to version 14" }));
    await user.click(within(screen.getByRole("alertdialog", { name: "Revert pipeline" })).getByRole("button", { name: "Revert" }));
    expect(revertToVersion).toHaveBeenCalledWith("state-14");
  });

  it("keyboard path: arrow to the group, Right expands, arrow into a member, Enter arms revert", async () => {
    const user = userEvent.setup();
    render(<HeaderVersionSelector />);
    await user.click(screen.getByRole("button", { name: /Composition history/ }));
    await user.keyboard("{ArrowDown}{ArrowRight}"); // focus v15–v18, expand
    await user.keyboard("{ArrowDown}{Enter}");      // focus v18, arm revert
    expect(screen.getByRole("alertdialog", { name: "Revert pipeline" })).toBeInTheDocument();
    expect(screen.getByRole("alertdialog")).toHaveTextContent("version 18");
  });

  it("renders every version flat with show_advanced on", async () => {
    usePreferencesStore.setState({ showAdvanced: true });
    const user = userEvent.setup();
    render(<HeaderVersionSelector />);
    await user.click(screen.getByRole("button", { name: /Composition history/ }));
    expect(screen.getAllByRole("treeitem")).toHaveLength(9);
  });
});
```

(Confirm the ConfirmDialog's confirm button name is `Revert` — `HeaderVersionSelector.tsx:353` `confirmLabel="Revert"`.)

- [ ] **Step 5: Implement the component**

`HeaderVersionSelector.tsx`:

1. Imports: `useShowAdvanced`, `buildVersionRows`, `versionLabelKind`, types. Two sites the role change reaches that are easy to miss: the scroll-into-view effect (`:141-145`) queries `querySelectorAll("[role='option']")` — change it to `"[role='treeitem']"` (document order matches `focusOrder` exactly, since a group's members follow it in the DOM, so the index arithmetic is unchanged; jsdom's `scrollIntoView` is a no-op, so no test would have caught the empty NodeList); and the trigger's `aria-haspopup="listbox"` (`:225`) becomes `aria-haspopup="tree"`.
2. State: replace `selectedIndex` with `const [selectedVersionNumber, setSelectedVersionNumber] = useState<number | null>(null);` (keep `focusedIndex` as the roving cursor over the *flattened* tree order); add `const [expandedGroups, setExpandedGroups] = useState<ReadonlySet<string>>(new Set());` and `const showAdvanced = useShowAdvanced();`. `toggle` resets `setSelectedVersionNumber(currentVersion)` and `setFocusedIndex(0)`.
3. Rows (after `sortedVersions`, `:54-80`):

```ts
  const kindFor = (version: CompositionStateVersion) =>
    isSnapshotOnly(version, findPredecessor(version))
      ? ("snapshot" as const)
      : versionLabelKind(version, stateVersions, messages);
  const rows = buildVersionRows(sortedVersions, kindFor, currentVersion, showAdvanced, expandedGroups);
  // Flattened visual/focus order: a group, then (when expanded) its members.
  const focusOrder: Array<{ row: VersionListRow; member?: CompositionStateVersion }> = rows.flatMap((row) =>
    row.kind === "group" && row.expanded
      ? [{ row }, ...row.versions.map((member) => ({ row, member }))]
      : [{ row }],
  );
  const focusedVersion = (index: number): CompositionStateVersion | null => {
    const entry = focusOrder[index];
    if (entry === undefined) return null;
    if (entry.member !== undefined) return entry.member;
    return entry.row.kind === "version" ? entry.row.version : null;
  };
  const selectedVersion =
    selectedVersionNumber === null
      ? null
      : (sortedVersions.find((v) => v.version === selectedVersionNumber) ?? null);
  const canRevertSelected = selectedVersion !== null && selectedVersion.version !== currentVersion;
```

The `:146-150` effect (which reset `selectedIndex` when it overflowed) becomes: clamp `focusedIndex` to `focusOrder.length - 1`, and if `selectedVersionNumber` no longer exists in `sortedVersions`, set it to `currentVersion`.
4. Keyboard (`:166-208`): `count = focusOrder.length`; ArrowDown/ArrowUp move `focusedIndex` only (selection no longer follows focus — selection is an explicit Enter/click on a version item, which is what makes the target stable). Add:

```ts
    const entry = focusOrder[focusedIndex];
    const toggleGroup = (id: string) =>
      setExpandedGroups((prev) => { const next = new Set(prev); next.has(id) ? next.delete(id) : next.add(id); return next; });
    if (e.key === "ArrowRight" && entry?.row.kind === "group" && entry.member === undefined && !entry.row.expanded) {
      e.preventDefault(); toggleGroup(entry.row.id); return;
    }
    if (e.key === "ArrowLeft" && entry?.row.kind === "group" && entry.member === undefined && entry.row.expanded) {
      e.preventDefault(); toggleGroup(entry.row.id); return;
    }
    if ((e.key === "Enter" || e.key === " ") && entry !== undefined) {
      e.preventDefault();
      if (entry.row.kind === "group" && entry.member === undefined) { toggleGroup(entry.row.id); return; }
      const version = focusedVersion(focusedIndex);
      if (version === null) return;
      setSelectedVersionNumber(version.version);
      if (version.version !== currentVersion) setRevertTarget(version);
    }
```

5. Rendering (`:244-330`): the `<ul>` becomes `role="tree"` (`aria-label="Composition history"` unchanged) with `aria-activedescendant` derived from `focusOrder` (the live expression at `:250-254` indexes `sortedVersions`, which is no longer the focus order once a group exists):

```tsx
            aria-activedescendant={(() => {
              const entry = focusOrder[focusedIndex];
              if (entry === undefined) return undefined;
              if (entry.member !== undefined) return `${listboxId}-option-${entry.member.version}`;
              return entry.row.kind === "group"
                ? `${listboxId}-group-${entry.row.id}`
                : `${listboxId}-option-${entry.row.version.version}`;
            })()}
``` A version item (top-level or member) renders the existing `<li>` verbatim except `role="treeitem"`, `aria-selected={selectedVersionNumber === version.version}`, `onClick={() => { setFocusedIndex(index); setSelectedVersionNumber(version.version); }}`. A group renders:

```tsx
                <li
                  key={row.id}
                  id={`${listboxId}-group-${row.id}`}
                  role="treeitem"
                  aria-expanded={row.expanded}
                  aria-selected={false}
                  aria-label={`Versions ${low} to ${high} — ${plural(row.versions.length, "edit")}`}
                  className={`version-selector-item version-selector-group${isFocused ? " version-selector-item--focused" : ""}`}
                  onClick={() => { setFocusedIndex(index); toggleGroup(row.id); }}
                  onMouseEnter={() => setFocusedIndex(index)}
                >
                  <span className="version-selector-item-info">
                    <span className="version-selector-item-label">v{low}–v{high}</span>
                    <span className="version-selector-item-meta">{plural(row.versions.length, "edit")}</span>
                    <span className="version-selector-item-meta">{relativeTime(row.versions[0].created_at)}</span>
                    <span aria-hidden="true">{row.expanded ? "▾" : "▸"}</span>
                  </span>
                  {row.expanded && (
                    <ul role="group" className="version-selector-group-members">
                      {row.versions.map((member) => …the version item markup, with its own focusOrder index…)}
                    </ul>
                  )}
                </li>
```

(`low`/`high` from the member version numbers; a member `<li>` is a nested `treeitem` inside the `role="group"` list — the ARIA tree pattern. `onMouseEnter` on the group must not swallow member hovers: members set their own `focusedIndex`.) The Revert button (`:330-346`) reads `selectedVersion` as before — now version-keyed.
6. `header.css`:

```css
.version-selector-group {
  font-weight: 600;
}
.version-selector-group-members {
  list-style: none;
  margin: 0;
  padding-left: 0.75rem;
}
```

- [ ] **Step 6: Run**

Run: `npx vitest run src/components/header src/test/a11y`
Expected: PASS — the renamed role queries and the new tests. Note the a11y suite is NOT evidence for the tree markup: `components.a11y.test.tsx:715-720` renders `<HeaderVersionSelector />` with no session-store setup, so the component returns `null` and axe runs on an empty container. The `ul[role=tree] > li[role=treeitem] > ul[role=group] > li[role=treeitem]` shape is the WAI-ARIA tree pattern; if the executor wants axe evidence, add a store-seeded render to that suite in this task.

- [ ] **Step 7: Commit**

```bash
git add src/elspeth/web/frontend/src/components/header/versionLabels.ts src/elspeth/web/frontend/src/components/header/versionLabels.test.ts src/elspeth/web/frontend/src/components/header/versionGrouping.ts src/elspeth/web/frontend/src/components/header/versionGrouping.test.ts src/elspeth/web/frontend/src/components/header/HeaderVersionSelector.tsx src/elspeth/web/frontend/src/components/header/HeaderVersionSelector.test.tsx src/elspeth/web/frontend/src/components/header/header.css src/elspeth/web/frontend/src/test/composerFixtures.ts
git commit -m "feat(header): version history is a tree; consecutive edits group and expand in place; revert target keyed by version (elspeth-c8a402a9a4)"
```

**Ticket disposition:** CLOSE at end of wave; the closing comment names the deliberate out-of-scope backend `source` field (ticket §3), the listbox→tree change, and offers a follow-up ticket for edit-source labels.

---

### Task 8: OptionRows — catalog-tier-driven ordering (`elspeth-a6ea581e8a` follow-up, roadmap row 8)

**Files:**
- Modify: `src/elspeth/web/frontend/src/types/index.ts:436-441` (`PluginSchemaInfo`)
- Modify: `src/elspeth/web/frontend/src/components/inspector/OptionRows.tsx`
- Modify: `src/elspeth/web/frontend/src/components/inspector/GraphView.tsx:605-660,:718`, `src/elspeth/web/frontend/src/components/workspace/PipelineSpecView.tsx:117,:128-210`
- Test: `OptionRows.test.tsx`, `GraphView.test.tsx`, `PipelineSpecView.test.tsx`; fixture literals gaining `knob_schema`: `src/api/client.catalog.test.ts`, `src/components/catalog/PluginCard.test.tsx`, `src/stores/pluginCatalogStore.test.ts`, `src/components/catalog/CatalogDrawer.test.tsx`

**Interfaces:**
- Consumes: `usePluginCatalogStore` (`schemas: Record<"kind:name", PluginSchemaInfo>` `:31`, `key: string | null` `:24`, `loadSchema(kind, name)` `:202-244` — returns early when `state.key === null` OR the key is cached/loading; `invalidate()` `:246-258` resets to `emptyCatalogState()` incl. `key: null` before reloading). The backend already ships `knob_schema` on the schema response (`web/catalog/schemas.py:105`); only the frontend type omitted it.
- Produces: `OptionRows` accepts `plugin?: { kind: "source" | "transform" | "sink"; name: string } | null`; orders/partitions by catalog tier when the schema is cached; falls back to an explicit static list otherwise. `OPTION_LABELS`, `optionLabel`, `INTERNAL_OPTION_KEYS`, blob masking (now value-bound for both partitions), and the empty sentence keep their pinned behaviour.
- Semantics: keys present in the schema with tier `"essential"`/`"common"` render visibly, essentials first, then schema field order; tier `"advanced"` and keys the schema does not know go under "Advanced settings (N)"; `INTERNAL_OPTION_KEYS` never render as rows.
- Cost note: the Spec tab renders one `OptionRows` per row (`PipelineSpecView.tsx:117`), so passing `plugin` fans out one `loadSchema` per row — deduped by the store to one request per *distinct* plugin (a 10-node pipeline with 6 plugin kinds issues 6 requests on first open, none afterwards). Accepted: the Spec tab is a review surface and the fetches are cached for the session.

- [ ] **Step 1: Expose the knob schema to the store's consumers**

`types/index.ts` — extend `PluginSchemaInfo` (import `FieldTier` from `./guided`):

```ts
/** One lowered composer knob as the inspector needs it — the catalog side of
 *  the same lowered field the guided form reads as KnobField (types/guided.ts).
 *  `tier` is REQUIRED here: the catalog lowering sets it on every field
 *  (knob_schema.py _attach_tier); only the guided-turn projection has a
 *  pre-tier durable story. */
export type CatalogKnobField = { name: string; tier: FieldTier };

export interface PluginSchemaInfo {
  name: string;
  plugin_type: "source" | "transform" | "sink";
  description: string;
  json_schema: Record<string, unknown>;
  /** Lowered composer knob schema — already on the wire (catalog/schemas.py),
   *  now typed so pluginCatalogStore exposes it to the inspector. */
  knob_schema: { fields: CatalogKnobField[] };
}
```

Run `npx tsc --noEmit -p .` — the four fixture files above gain `knob_schema: { fields: [] }` on each `PluginSchemaInfo` literal.

- [ ] **Step 2: Failing OptionRows tests**

`OptionRows.test.tsx` — add `usePluginCatalogStore` import; the top-level `beforeEach` resets BOTH stores (`resetStore(usePreferencesStore); resetStore(usePluginCatalogStore);`). The file's `OPTIONS` fixture (`:8-15`) gains one key the schema does not know: `max_retries: 3`. Note the Wave 1 tests that pin `"Advanced settings (1)"` now see `max_retries` in the fallback's advanced bucket too — update those pins to `(2)` (the fallback partition: visible = label-map keys present; advanced = everything else non-internal, i.e. `temperature` + `max_retries`). Append:

```tsx
describe("catalog-tier ordering (elspeth-a6ea581e8a follow-up)", () => {
  const LLM_SCHEMA = {
    name: "llm",
    plugin_type: "transform",
    description: "",
    json_schema: {},
    knob_schema: {
      fields: [
        { name: "profile", tier: "common" },
        { name: "prompt_template", tier: "common" },
        { name: "temperature", tier: "advanced" },
        { name: "schema", tier: "common" },
      ],
    },
  } as const;
  const seedCatalog = (schemas: Record<string, unknown>) =>
    usePluginCatalogStore.setState({ key: "alice:fp-1", principal: "alice", fingerprint: "fp-1", schemas } as never);

  it("orders visible rows by the schema and sends advanced-tier + unknown keys to the disclosure", () => {
    seedCatalog({ "transform:llm": LLM_SCHEMA });
    render(<OptionRows options={OPTIONS} ariaLabel="assess options" plugin={{ kind: "transform", name: "llm" }} />);
    const region = screen.getByRole("region", { name: "assess options" });
    // `.graph-config-nested` excluded: OPTIONS.schema is a record, and ConfigValue
    // renders its keys as nested <dt>s (ConfigRows.tsx:41-52) in the visible partition.
    const visibleTerms = within(region).getAllByRole("term").filter((t) => t.closest("details") === null && t.closest(".graph-config-nested") === null).map((t) => t.textContent);
    // Schema field order — DIFFERENT from the fallback's label-map order
    // (["Prompt", "Model profile", "Row schema"]); this is the oracle that
    // distinguishes the two partitions.
    expect(visibleTerms).toEqual(["Model profile", "Prompt", "Row schema"]);
    const advanced = within(region).getByText("Advanced settings (2)").closest("details") as HTMLElement;
    expect(within(advanced).getByText("Temperature")).toBeInTheDocument(); // advanced tier
    expect(within(advanced).getByText("Max Retries")).toBeInTheDocument(); // unknown to the schema
    expect(region.textContent).not.toMatch(/blob_ref|interpretation_requirements/);
  });

  it("falls back to the static split when the schema is not cached (regression pin — green before this task)", () => {
    render(<OptionRows options={OPTIONS} ariaLabel="assess options" plugin={{ kind: "transform", name: "llm" }} />);
    const region = screen.getByRole("region", { name: "assess options" });
    const visibleTerms = within(region).getAllByRole("term").filter((t) => t.closest("details") === null && t.closest(".graph-config-nested") === null).map((t) => t.textContent);
    expect(visibleTerms).toEqual(["Prompt", "Model profile", "Row schema"]);
  });

  it("re-partitions when the catalog loads after mount (no request is made before the catalog has a key)", () => {
    const loadSchema = vi.fn().mockResolvedValue(undefined);
    usePluginCatalogStore.setState({ loadSchema } as never);
    render(<OptionRows options={OPTIONS} ariaLabel="assess options" plugin={{ kind: "transform", name: "llm" }} />);
    expect(loadSchema).not.toHaveBeenCalled(); // key is null: the store would no-op; we don't even ask
    act(() => seedCatalog({ "transform:llm": LLM_SCHEMA }));
    expect(loadSchema).toHaveBeenCalledWith("transform", "llm");
    const region = screen.getByRole("region", { name: "assess options" });
    const visibleTerms = within(region).getAllByRole("term").filter((t) => t.closest("details") === null && t.closest(".graph-config-nested") === null).map((t) => t.textContent);
    expect(visibleTerms).toEqual(["Model profile", "Prompt", "Row schema"]);
  });

  it("masks a blob:<ref> path even when the catalog tiers `path` advanced (masking binds to the value, not the partition)", () => {
    seedCatalog({
      "source:csv": { ...LLM_SCHEMA, name: "csv", plugin_type: "source", knob_schema: { fields: [{ name: "path", tier: "advanced" }] } },
    });
    render(
      <OptionRows
        options={{ path: "blob:f976fd8b-4432-4f8f-bbc3-2d8a9f2114e0" }}
        ariaLabel="source options"
        plugin={{ kind: "source", name: "csv" }}
      />,
    );
    const region = screen.getByRole("region", { name: "source options" });
    const advanced = within(region).getByText("Advanced settings (1)").closest("details") as HTMLElement;
    expect(within(advanced).getByText("Uploaded sample data")).toHaveAttribute("title", "blob:f976fd8b-4432-4f8f-bbc3-2d8a9f2114e0");
    expect(region.textContent).not.toMatch(/f976fd8b-4432/);
  });
});
```

Run → FAIL (no `plugin` prop).

- [ ] **Step 3: Implement**

`OptionRows.tsx` (imports: add `useEffect` from react, `usePluginCatalogStore` from `@/stores/pluginCatalogStore`; `ConfigRows` is no longer used — drop it, keep `ConfigValue`):

1. Replace the `ESSENTIAL_OPTION_KEYS` derivation (`:60`) with an explicit list, decoupled from the label map (adding a label for copy reasons must not promote a key into the fallback's visible set):

```ts
// Fallback partition when the plugin's catalog schema is not cached (catalog
// never loaded, invalidated, structural node). Declared explicitly — NOT
// derived from OPTION_LABELS — so a copy edit cannot change what is visible.
export const FALLBACK_VISIBLE_OPTION_KEYS: readonly string[] = [
  "prompt_template", "system_prompt", "profile", "model", "response_field", "path",
  "schema", "mode", "fields", "field_mapping", "select_only", "columns", "url", "query",
];
```

(Same 14 keys in the same order as today's `Object.keys(OPTION_LABELS)`, so every Wave 1 fallback pin holds.) Update the header comment: tiers come from the catalog schema; the static list is the not-yet-loaded fallback. Update the `GraphView.test.tsx:358-359` comment reference.
2. Rename `EssentialValue` → `OptionValue` (body unchanged: masks the `blob:` sentinel on the `File` label, else `<ConfigValue value={value} humaniseKeys={STRUCTURAL_OPTION_CONTAINER_LABELS.has(label)} />` — the 900b86b8f fail-closed nested-key rule, now reaching both partitions) and render BOTH partitions through the same mapped `<dl>` so masking is a property of the value:

```tsx
function OptionDl({ rows }: { rows: Record<string, unknown> }): JSX.Element {
  return (
    <dl className="graph-config-rows">
      {Object.entries(rows).map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd><OptionValue label={label} value={value} /></dd>
        </div>
      ))}
    </dl>
  );
}
```

3. Props + schema subscription:

```tsx
export function OptionRows({ options, ariaLabel, plugin = null }: {
  options: Record<string, unknown>;
  ariaLabel: string;
  plugin?: { kind: "source" | "transform" | "sink"; name: string } | null;
}): JSX.Element {
  const showAdvanced = useShowAdvanced();
  const catalogKey = usePluginCatalogStore((s) => s.key);
  const schema = usePluginCatalogStore((s) =>
    plugin === null ? undefined : s.schemas[`${plugin.kind}:${plugin.name}`],
  );
  const loadSchema = usePluginCatalogStore((s) => s.loadSchema);
  useEffect(() => {
    // loadSchema no-ops on cached/loading keys and makes NO request while the
    // catalog has no key. Subscribing to `key` re-fires this on first catalog
    // load and after invalidate() (which wipes `schemas` and `key` together),
    // so a mounted inspector recovers instead of silently living on the
    // fallback partition.
    if (plugin !== null && catalogKey !== null) void loadSchema(plugin.kind, plugin.name);
  }, [plugin?.kind, plugin?.name, catalogKey, loadSchema]);
```

4. Partition:

```tsx
  const candidateKeys = Object.keys(options).filter((key) => !INTERNAL_OPTION_KEYS.has(key));
  let visibleKeys: string[];
  let advancedKeys: string[];
  if (schema === undefined) {
    visibleKeys = FALLBACK_VISIBLE_OPTION_KEYS.filter((key) => Object.prototype.hasOwnProperty.call(options, key));
    advancedKeys = candidateKeys.filter((key) => !FALLBACK_VISIBLE_OPTION_KEYS.includes(key));
  } else {
    const tierByKey = new Map(schema.knob_schema.fields.map((field) => [field.name, field.tier]));
    const schemaOrder = schema.knob_schema.fields.map((field) => field.name);
    const present = (key: string): boolean => candidateKeys.includes(key);
    const essentials = schemaOrder.filter((key) => present(key) && tierByKey.get(key) === "essential");
    const commons = schemaOrder.filter((key) => present(key) && tierByKey.get(key) === "common");
    visibleKeys = [...essentials, ...commons];
    advancedKeys = candidateKeys.filter((key) => !visibleKeys.includes(key));
  }
  const visible = pick(options, visibleKeys);
  const advanced = pick(options, advancedKeys);
```

**Nested-key humanising is fail-closed (Wave 1 round 3, 900b86b8f) and the swap must keep it wired for BOTH partitions:** `ConfigValue` takes `humaniseKeys?: boolean` (default false), `ConfigRows` takes `structuralKeys?: ReadonlySet<string>`, and `STRUCTURAL_OPTION_CONTAINER_KEYS` (`{schema, output_schema, input_schema}`) is exported from `ConfigRows.tsx:36`; today `OptionRows.tsx` maps it by LABEL (`STRUCTURAL_OPTION_CONTAINER_LABELS`, `:62-68`) and passes `humaniseKeys={STRUCTURAL_OPTION_CONTAINER_LABELS.has(label)}` for essential rows (`:103`) and `structuralKeys={STRUCTURAL_OPTION_CONTAINER_LABELS}` to the advanced `ConfigRows` (`:142`). After the swap `OptionValue` (renamed `EssentialValue`, `:95-104`) renders both partitions through `OptionDl`, so its `humaniseKeys={STRUCTURAL_OPTION_CONTAINER_LABELS.has(label)}` line is the single site and applies to essential AND advanced rows alike — do not drop it, and do not widen it. User-keyed maps (`field_mapping`, `lookups`, `headers_custom_mapping`) render verbatim; the pins in `OptionRows.test.tsx` and `PipelineSpecView.test.tsx` must stay green untouched. The render body: `<OptionDl rows={visible} />` when non-empty; `<details className="option-rows-advanced" open={showAdvanced}><summary>Advanced settings ({advancedKeys.length})</summary><OptionDl rows={advanced} /></details>` when `advancedKeys.length > 0`; the empty sentence and the raw-JSON block unchanged.
5. Call sites. `GraphView.tsx` `selectedComponentConfig` (`:605-660`) already carries `plugin: string | null` per arm; add `pluginKind: "source" | "transform" | "sink"` to each arm (`"source"` / `"transform"` / `"sink"`) and `:718` becomes `plugin={config.plugin === null ? null : { kind: config.pluginKind, name: config.plugin }}`. `PipelineSpecView.tsx` — `SpecRow` has `kind` (`"source"` / node_type / `"output"`) and `plugin`; map `source→"source"`, `output→"sink"`, else `"transform"`, and pass the same prop at `:117`.
6. Store resets: add `resetStore(usePluginCatalogStore)` to the top-level `beforeEach` of `GraphView.test.tsx` (`:279`) and `PipelineSpecView.test.tsx` (`:9`), alongside `resetStore(usePreferencesStore)` if not already there — both render `OptionRows`, which now reads two stores.

- [ ] **Step 4: Run**

Run: `npx vitest run src/components/inspector src/components/workspace src/stores/pluginCatalogStore.test.ts src/components/catalog src/api/client.catalog.test.ts` → PASS (every Wave 1 masking/label pin holds; the `(1)`→`(2)` count pins updated in Step 2). Then `npx tsc --noEmit -p .` → clean.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/web/frontend/src/types/index.ts src/elspeth/web/frontend/src/components/inspector/OptionRows.tsx src/elspeth/web/frontend/src/components/inspector/OptionRows.test.tsx src/elspeth/web/frontend/src/components/inspector/GraphView.tsx src/elspeth/web/frontend/src/components/inspector/GraphView.test.tsx src/elspeth/web/frontend/src/components/workspace/PipelineSpecView.tsx src/elspeth/web/frontend/src/components/workspace/PipelineSpecView.test.tsx src/elspeth/web/frontend/src/api/client.catalog.test.ts src/elspeth/web/frontend/src/components/catalog/PluginCard.test.tsx src/elspeth/web/frontend/src/stores/pluginCatalogStore.test.ts src/elspeth/web/frontend/src/components/catalog/CatalogDrawer.test.tsx
git commit -m "feat(inspector): OptionRows orders by catalog tier when the schema is cached; value-bound blob masking; explicit static fallback (elspeth-a6ea581e8a)"
```

**Ticket disposition:** CLOSE at end of wave — the Wave 1 comment stream says the ticket stays open only for exactly this follow-up.

---

### Task 9: Whole-tree verification and closeout

**Files:** none new. Runs on the merged integration branch (all eight task branches landed), not on any single lane's branch.

- [ ] **Step 1a: Frontend full run**

From `src/elspeth/web/frontend`: `npx vitest run`, then `npx tsc --noEmit -p .`, then `npm run lint`. Expected: all green, including `src/styles/classNames.test.ts` (every class added in Tasks 1–8 has a rule), the two directory gates, and the a11y suite.

- [ ] **Step 1b: Frontend e2e (sequential, one worktree)**

With the local stack up (`docs/agents`/`examples/AGENTS.md` describe the runnable staging surfaces; credentials never in docs): `npx playwright test tests/e2e/composer-workspace-geometry.spec.ts tests/e2e/composer-preferences.spec.ts tests/e2e/composer-workspace-accessibility.spec.ts` — the Task 3 preference seeding, the default-preference completion bar, and axe over the changed surfaces. Never run two Playwright commands concurrently in one worktree.

- [ ] **Step 2: Lint corpus diff (backend-touching: Tasks 1 and 5)**

```bash
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules all --root src/elspeth > /tmp/claude-1000/-home-john-elspeth/w2-lints-after.txt
diff /tmp/claude-1000/-home-john-elspeth/w2-lints-before.txt /tmp/claude-1000/-home-john-elspeth/w2-lints-after.txt; grep -c . /tmp/claude-1000/-home-john-elspeth/w2-lints-after.txt  # grep -c prints 0 and exits 1 on zero matches — do not wrap in set -e
```

Expected: no added findings, identical counts (COUNT the corpus, never `tail` it). Task 5's `protocol.py` edits use the file's existing Mapping-membership parse idiom under the standing `@trust_boundary` decorators — no new dynamic-attribute sites; if the diff disagrees, the code is wrong, not the gate.

- [ ] **Step 3: Backend full suite as a background worktree job**

```bash
git worktree add .claude/worktrees/wave2-verify HEAD
ln -s "$(pwd)/.venv" .claude/worktrees/wave2-verify/.venv 2>/dev/null || true
cd .claude/worktrees/wave2-verify
PYTHONPATH=$(pwd)/src:$(pwd)/elspeth-lints/src .venv/bin/python -c "import elspeth, elspeth_lints; print(elspeth.__file__, elspeth_lints.__file__)"  # both must point into the worktree
PYTHONPATH=$(pwd)/src:$(pwd)/elspeth-lints/src .venv/bin/python -m pytest tests/ -n 12 -q 2>&1 | tail -30
```

Run in the background; read the summary line, not `tail` alone (three void-run variants exist — confirm a non-zero collected count). Must-be-green list: `tests/unit/web/test_sessions_composer_attribute_contracts.py`, `tests/unit/elspeth_lints/test_masquerade_gate.py`, `tests/unit/web/catalog/test_knob_schema_golden.py` (goldens untouched), `tests/unit/web/catalog/test_guided_option_tier_parity.py`, `tests/unit/web/composer/test_tool_call_description_parity.py`, and every suite referencing `node_options_summary`: `tests/integration/web/composer/guided/test_proposal_audit_projection.py`, `tests/unit/web/composer/test_proposals.py`, `tests/unit/web/composer/guided/test_bind_reviewed_components.py`, `test_emitters.py`, `test_propose_pipeline_protocol.py`, `test_protocol.py`, `test_collector_guard.py`, `test_chat_solver.py`, `tests/unit/web/aws_ecs_acceptance/test_receipt_contracts.py`, `tests/unit/web/aws_ecs_acceptance/test_cleanup_control_service.py`.

- [ ] **Step 4: Live check on session 39578c6f**

Executor: the hub, on the merged integration branch. Build (`npm run build` in `src/elspeth/web/frontend`), `sudo systemctl restart elspeth-web`, then poll `/api/system/status` until `frontend_build` shows the new build id (`is-active` lies after a restart). Sign in as the staging operator account; open session `39578c6f`. With the default preference, then again after toggling "Show technical detail":

1. A settled tool card reads "Applied: <sentence>" with the raw name in mono; no bare snake_case primary anywhere in the transcript.
2. Run history: no UUID visible or announced by default; "Show detail" shows count + Explain + curated failure rows, and the token/state lists only with the flag.
3. Completion bar: two gestures by default; Import YAML appears with the flag.
4. Post-run view: closure sentence + "Accounting detail" disclosure; errors summarised to a count.
5. A wire-stage row: no cardinality enums or raw plugin ids in the main flow; per-row Technical details opens with the flag; run the tutorial once — ADR-031 canary, unchanged.
6. Catalog: ≤3 audit chips and no Schema button by default; an open Schema panel closes when the flag goes off.
7. v19 version dropdown: exactly 4 top-level items with groups expanding in place; select v14, expand v15–v18, the Revert button still says v14; flag renders all rows flat.
8. Node inspector + Spec tab options order by catalog tier once the catalog has loaded (open the catalog drawer first, then re-open the inspector); the Spec tab's `Plugin` row and the inspector's plugin line read display names (Task 5).

**Spec-tab scope of this live check (from the Wave 1 live check, Check 1, 83 snake_case hits at the default preference):** Wave 2 clears only the plugin-id rows. The remaining Spec-tab residue is NOT expected to clear in this wave and must not be reported as a Wave 2 failure — it is homed in Wave 3 (see the roadmap table): every node card's `<h4>` heading is the raw node id (13 headings); routing `<dd>` values carry wire-stage/branch names verbatim (`raw_rows`, `invest_cs1_done`, `branch_invest_cs1`); policy enums (`require_all`, `bind_source`) render as plain values. The residual baseline for this check is recorded in the Wave 1 epic comment (from the post-3b7281965 re-walk); compare against it, do not re-derive a number here.

Report: one `filigree add-comment elspeth-cd8abcba3f` listing each check pass/fail with the frontend build id and the merged sha; a failed check REOPENS that task's ticket rather than being noted in prose.

- [ ] **Step 5: Ticket mechanics**

All 8 are type `task` (working status `in_progress`), claimed at task start with `filigree start-work <id> --assignee <lane>` after Task 0 unblocked them. At closeout, for each of `elspeth-af559a0bab`, `elspeth-34e810312c`, `elspeth-aa39cffb16`, `elspeth-05a240b82a`, `elspeth-ca456d9d8d`, `elspeth-8555a6a9e0`, `elspeth-c8a402a9a4`, `elspeth-a6ea581e8a`:

```bash
filigree add-comment <id> "<what landed, commit shas, what was verified (tests + live check)>"
filigree close <id>
```

Specifics: `elspeth-af559a0bab`'s comment records that its tool list was verified against the imported registry (already corrected in the ticket text; no edit needed) and names the parity test. `elspeth-ca456d9d8d`'s comment records the two design decisions (tier in the allowlist; emit-always/admit-optional with the zero-row sweep result and no epoch bump). `elspeth-c8a402a9a4`'s comment records the listbox→tree change, the version-keyed selection, and the deliberate out-of-scope backend `source` field with an offered follow-up ticket. `elspeth-a6ea581e8a` CLOSES — its Wave 1 comments held it open solely for this row-8 follow-up. The epic `elspeth-cd8abcba3f` stays open (Wave 3 children remain); the Wave 1 deferred minor "preferencesStore.test.ts tutorial-resume describe placement" was NOT absorbed (no Wave 2 task touches that file) and rides to Wave 3, as does the aria-describedby-to-closed-details AT verification (real-screen-reader follow-up — out of scope here by memo).

---

## Roadmap: Wave 3 (separate plan; unchanged from the Wave 1 plan)

**Wave 3 — register, bugs, cleanup** (independent of the flag; can run in parallel with Wave 2 by a second lane):

| Ticket | Scope |
|---|---|
| `elspeth-93f5621f18` | `humaniseStepLabel` raw-id fallback → "a removed step" (design reversal; the :120-124 doctrine and two pinning tests change with it). **Also owns (live-check addendum, Spec tab at the default preference):** routing `<dd>` values that are wire-stage/branch names verbatim (`raw_rows`, `invest_cs1_done`, `branch_invest_cs1` — `PipelineSpecView.tsx` `routingValue`, the `routingLabel` snake_case fallback the memo already homed here) — resolve through the phrase map / `stepLabelForNodeId`, raw ids to `title`. (The raw-JSON `branches` mapping the live check also saw was fixed in Wave 1 at 3b7281965: `routingValue` now renders `branches` maps as prose like `routes`.) |
| `elspeth-d74ab492dd` | Register batch (ModelChip, scope badge, byte count, failure enums, ExplainDialog, egress plugin names, provenance enum, "reviewed" word). **Also owns (live-check addendum):** the Spec tab node-card `<h4>` headings that render the raw node id (`PipelineSpecView.tsx:93` — 13 of the 83 default-preference hits on 39578c6f; phrase-map label visible, raw id in `title`/`aria-label`), and the policy-enum `<dd>` values (`require_all`, `bind_source` — coalesce/scope policy vocabulary, the same class as the batch's failure enums) |
| `elspeth-4bf65fe149` | Planner brief: reader's terms; corpus case that fails on `is_valid:`/`options.` tokens |
| `elspeth-d1feee1e67` | e2e keyboard path through the graph a11y list (the 1px clip + `:focus-within` reveal is deliberate; 57c6fba409 closed not_a_bug) |
| `elspeth-f1394307e3` | Minor gated surfaces: recovery transcript, blob structural disclosure, audit Refresh (Show archived dropped — it is the only archive-restore path) |
| `elspeth-0bfd019f68` (remainder) | Delete the unknown-audit-characteristic chip (the vocabulary-parity test already exists: test_audit_characteristic_vocabulary_parity.py:38) |

Also riding to Wave 3 from Wave 2's deferrals: the `!nodes` raw-id fallback + `routingLabel` snake_case fallback (homed in `elspeth-93f5621f18`'s batch), the preferencesStore.test.ts describe-block move, the closed-`<details>` aria-describedby AT verification, and (if wanted) the backend `source` field for version edit-source labels.

**Explicitly not owned by any Wave 2 row (so it is not silently dropped):** of the Spec-tab register residue the Wave 1 live check found at the default preference (`.superpowers/sdd/2026-08-29-composer-detail-level-wave1/live-check-report.md` §Check 1 — 83 snake_case text nodes outside `<code>`/`<pre>`/`<details>`), Wave 2 fixes only the plugin-id rows (row 5). The node-id headings and policy enums go to `elspeth-d74ab492dd`; the wire-stage/branch routing values go to `elspeth-93f5621f18`. Two of the live check's items were fixed in Wave 1 after the check (3b7281965): the raw-JSON `branches` blob (`routingValue` prose) and nested option KEYS in `<dt>` (`guaranteed_fields`, `fields` — `ConfigRows.ConfigValue` now runs structural nested keys through `titleCaseLabel` with the raw key in `title`); Tasks 5 and 8 must preserve both (their pins live in `PipelineSpecView.test.tsx` and `OptionRows.test.tsx`). One item has no owner at all and is recorded here for the Wave 3 plan to place: field names inside prompt-template excerpts (`case_study1`) — authored content that may be correct as-is; Wave 3 decides.

**Sequencing rule (both waves):** one PR per ticket; each PR's default-DOM regression pin (`expectNoIdentifiersInDefaultDom`) is the acceptance test the reviewer runs first.
