# Advisor Multi-Query Evidence Fix — Close-Out Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close out the END-gate advisor evidence-blindness defect (session `94f6f00c`, F1 in the prior LLM diagnostician report) for multi-query LLM nodes, verify the implementation already staged in the working tree matches the SME review's recommendations, and fix the one gap that implementation does not close.

**Architecture:** No new architecture. This extends the existing `_summarize_pipeline_for_advisor` / `_render_options_for_advisor` / `_advisor_prompt_option_values` evidence pipeline in `src/elspeth/web/composer/service.py` with one new single-source-of-truth helper (`_advisor_query_option_values`) that both the renderer and the deterministic injection pre-scan walk, plus a companion helper (`_node_effective_prompt_templates`) that fixes the degeneracy signal to read live templates instead of a dead node-level slot. This is server-side *redaction/evidence rendering*, not pipeline authoring — it does not touch the Composer invariants (no server-authored pipeline structure; no tutorial-special path).

**Tech Stack:** Python 3.12, pytest, the existing `observation_boundary` Tier-3 boundary decorator convention.

**Spec:** SME review at `docs/plans/2026-09-13-advisor-multiquery-evidence-sme-review.md` (this plan's source; read it first — it has full evidence citations for every claim below). Prior diagnosis: session `8673059d`'s diagnostician report (finding F1; scratchpad-only, not durably filed as of this plan's writing).

## Global Constraints

- Never widen what the advisor can see beyond the render/scan single-source-of-truth pattern already established for `_advisor_control_flow_fields` (elspeth-eacfec09a6): one helper, walked by both the renderer and the injection pre-scan, pinned by a disagreement test. Do not hand-enumerate the same field set twice.
- Never phrase a withheld/dead-slot label as an editorial judgement ("dead", "leftover", "unused code") — fact-register only ("not used", "used by"). The rubric owns prompt correctness, not composer hygiene, and a judgement word primes the advisor to FLAG an inert-but-honest field as a defect in itself.
- Never FLAG, or teach the judge to FLAG, on a withheld or bounded-out value — the rubric's existing carve-out ("never FLAG an option, field, or contract merely because its value or entry is withheld") must keep covering every new `additional_*_withheld` counter this work adds.
- This is composer evidence-rendering code, not pipeline authoring: it does not touch, and must not be made to touch, the two Composer invariants (no server-authored pipeline structure; no tutorial-special path). If any task here starts to look like it needs either, stop and escalate rather than build it.
- Follow this repo's editing rules: no `sed`/`awk` multi-line rewrites, no `# noqa`/`# type: ignore`, Edit tool or full-file regeneration only.

---

## Measured close-out (2026-09-14, session 084a8817)

Every Task 1–5 check below was run literally by symbol (the plan's line numbers
predate the Textract gating edit) from a script; each printed PASS. Task 6 was
implemented as option (a) on the maintainer's "address all findings"
instruction. Raw results: session scratchpad `plan_measurement.txt`.

| Task | Check | Result |
|---|---|---|
| 1 | import, `queries`/`system_prompt` in both key sets, no-blob comment | PASS ×4 |
| 2 | cap 8 + markers, fact-register wording, scope label with no double render, per-query system prompt at `transform.py` multi-query branch, beyond-cap in-use accounting | PASS ×5 |
| 3 | scan wiring, injection preflight (positive + negative controls), render/scan disagreement pin | PASS ×3 |
| 4 | rubric names effective prompt text, general mismatch sentence byte-identical to HEAD, rubric test | PASS ×3 |
| 5 | dead node-level template excluded, both divergent fixture directions, advisor file serial | PASS ×3 |
| 6 | whole-line checkpoint bound at `composer_advisor_max_prompt_tokens × 4` with `additional_evidence_lines_withheld=N`; identity hashes stay over the unbounded summary; inert on ordinary pipelines | PASS |
| 7 | full-suite gate on a frozen isolated worktree (ruff, mypy, pytest, testcontainer) | run 1 (`20260913T145216Z-gate-tree2`): `frozen=yes`, ruff 0, mypy 0, testcontainer 0 (491 passed), pytest 4 failed / 52,699 passed — all four were hand-built multi-query fixtures whose stored review hash was the legacy node-level anchor, so the new fail-closed execution guard fired on them (the guard's positive control). Fixed by deriving the fixture anchor from `prompt_review_anchor_hash_from_options` in `tests/unit/web/execution/test_service.py` (3 fixtures) and `tests/integration/web/composer/parity/test_readiness_preflight_profile_lowering.py` (1). Run 2 (`20260913T153732Z-gate-tree3`, same HEAD + the 15-file diff; the script selected ruff+pytest since `src/` was byte-identical to run 1, whose mypy 0 and testcontainer 0 therefore stand): `frozen=yes`, ruff 0, pytest 1 failed / 52,702 passed. The one red, `tests/integration/engine/test_two_process_scheduler_contention.py::…first_claim_lands_after_its_window`, is an SQLite `database is locked` in a two-process claim hammer with another session's full `-n 12` suite on the box; it imports nothing under `web/`, passed in run 1 on identical source, and passed on serial re-run (`-n 0`, exit 0) per the AGENTS.md flake rule. Net: every test touched by this change is green across both runs |

Beyond this plan, the same close-out fixed the two other prior-report findings:
the `llm_prompt_template` review now anchors to and drafts the whole multi-query
prompt surface (`MultiQueryPromptSurface`), and the planner teaching now says
`{{ row.<variable> }}`. No legacy-anchor recognition was kept: the session
database is recreated when this fix is committed, so a stored review whose
anchor differs from `prompt_review_anchor_hash_from_options` is ordinary drift.

## Status Check (READ THIS BEFORE STARTING ANY TASK)

**Every task in this plan is implemented, uncommitted, in the working tree of
`release/0.8.1`, together with a wider composer-hardening change that grew out
of the same incident.** Round 1 of the evidence fix is on HEAD as `818d04577`.
Round 2 (the prompt-surface review anchor and draft, the checkpoint summary
bound, the planner teaching fix) and the hardening that followed it are not
committed. Measure the current shape before trusting this section:

```bash
cd "$(git rev-parse --show-toplevel)" && git log --oneline -1 && git diff --stat -- src tests config | tail -1
```

What the uncommitted working set adds beyond Tasks 1–6, grouped by concern:

- **Review card for multi-query nodes.** The card surfacer, the pending-event
  writer and the resolver share `prompt_review_draft_from_options`; single-prompt
  drafts are bounded to 8000 characters for the 8192-character wire cap. The
  review card renders the complete live prompt surface from node options, so
  the bounded draft never hides attested query text.
- **No legacy anchors.** `legacy_prompt_review_anchor_hashes` and the unwired
  reopen transform were removed. Any anchor mismatch is drift, raised as
  `InterpretationReviewIntegrityError`: a structured 409 on Execute and a
  readiness blocker on validate.
- **LLM-authored inline blobs in prompt fields.** One predicate in
  `execution/_validation_materialization.py` refuses them on validate, at run
  admission, and in `wire_blob_inline_ref`, `upsert_node`, `patch_node_options`,
  `splice_transform` and `set_pipeline`. User-uploaded blobs keep ADR-034
  behaviour.
- **Advisor gate.** The CLEAN verdict parser accepts the ASCII double-hyphen dash
  and no longer signs off on `clean.csv is the input`.
- **Composer robustness.** The heartbeat's retry limit is time-based and lease
  loss is recorded as a server fault on every compose route. Import refuses
  hand-written resolved review rows. Proposal-cap failures report real turn
  counts. The tool-argument guidance and error copy were corrected.

Plan and triage evidence for the hardening lives in the session scratchpad of
the implementing session, not in this repository.

Live check on the dev deploy (2026-09-15, fresh session database): a composed
two-query colour pipeline raised its prompt review card with the full query
surface, validate stayed blocked until the card was accepted, and the run then
completed with 5 of 5 rows written.

**Coordination note:** this is a shared checkout. Commit by explicit pathspec
(`git commit -- <paths>`), never a broad `git add`, and never `git restore` or
`git clean` a file you did not stage. Run `scripts/branch-safety-check.sh
--intent commit` first, re-pin the soft-mapping census in the same commit
(`python scripts/check_contracts.py --write-census`), and rename the served
session database when the commit lands (every local account then needs
`elspeth composer users bootstrap-admin` or the identity admin API; see
`docs/runbooks/staging-session-db-recreation.md`).

---

### Task 1: Verify the render/scan allowlist widening (Q A, Q B foundation)

**Files:**
- Verify: `src/elspeth/web/composer/service.py:203` (import), `:9425-9432` (`_ADVISOR_SUMMARY_VALUE_KEYS`), `:9495-9497` (`_ADVISOR_SUMMARY_PROMPT_VALUE_KEYS`)

**Interfaces:**
- Consumes: `_well_formed_query_entries` from `elspeth.web.composer.state` (already public enough to import — confirmed used at `service.py:203`).
- Produces: `queries` and `system_prompt` are now render-admitted keys; nothing downstream should special-case their absence any more.

- [ ] **Step 1: Confirm the import and allowlist entries are present**

```bash
cd "$(git rev-parse --show-toplevel)" && grep -n "_well_formed_query_entries" src/elspeth/web/composer/service.py
```

Expected: the import at line 203, plus three usages inside `_advisor_query_option_values` and `_node_effective_prompt_templates`.

```bash
cd "$(git rev-parse --show-toplevel)" && sed -n '9415,9500p' src/elspeth/web/composer/service.py
```

Expected: `"queries"` and `"system_prompt"` present in `_ADVISOR_SUMMARY_VALUE_KEYS`, and `"system_prompt"` added to `_ADVISOR_SUMMARY_PROMPT_VALUE_KEYS` (alongside `prompt_template`, `template`).

- [ ] **Step 2: Confirm `queries` itself is never rendered as one blob**

Read the code comment directly above the allowlist entries (`service.py:9425-9432`) and confirm it says `queries` is "expanded per query by `_advisor_query_option_values`, never rendered as one blob." This is the load-bearing design fact: `queries` being *admitted* does not mean it is rendered structurally as a dict dump (which would defeat the prompt/structural distinction the injection scanner depends on).

- [ ] **Step 3: No commit needed** — this task is verification-only. If any expectation above fails, STOP and escalate to whoever owns the in-flight diff before writing new code; do not silently patch over a partially-applied change.

---

### Task 2: Verify the single-source-of-truth helper (`_advisor_query_option_values`) and its render/scan wiring (Q A, Q B, Q C)

**Files:**
- Verify: `src/elspeth/web/composer/service.py:9506-9596` (`_advisor_query_option_values`), `:9877-9887` (its use inside `_render_options_for_advisor`), `:9258-9280` (its use inside `_advisor_prompt_option_values`)

**Interfaces:**
- Consumes: `options: Mapping[str, Any]` (an LLM node's options mapping).
- Produces: `list[tuple[str, str, bool]]` — `(label, text, prose_shaped)` triples, in the same convention as `_advisor_control_flow_fields` and `_advisor_prompt_option_values`. Labels observed: `queries.<name>.input_fields`, `queries.<name>.template`, `additional_queries_withheld`, `prompt_template_in_use`, `system_prompt_scope`.

- [ ] **Step 1: Confirm the cap and markers match the review's recommendation**

```bash
cd "$(git rev-parse --show-toplevel)" && sed -n '9494,9504p' src/elspeth/web/composer/service.py
```

Expected: `_ADVISOR_SUMMARY_MAX_QUERY_TEMPLATES: Final[int] = 8` (matches the `_render_schema_for_advisor` precedent this review cited), `_ADVISOR_SUMMARY_INVALID_QUERIES_MARKER`, `_ADVISOR_SUMMARY_QUERY_USES_NODE_TEMPLATE_MARKER = "(node-level prompt_template)"`.

- [ ] **Step 2: Confirm the dead-slot label wording is fact-register, not editorial**

```bash
cd "$(git rev-parse --show-toplevel)" && sed -n '9581,9596p' src/elspeth/web/composer/service.py
```

Expected wording: `"not used (every query supplies its own template)"` when no query falls back, `"queries without their own template: " + ", ".join(node_template_users)` when some do. Confirm the docstring's own comment explains why ("Fact-register wording … never an editorial 'dead'/'leftover'").

- [ ] **Step 3: Confirm `system_prompt` gets a scope label, not a duplicate render**

Confirm `_advisor_query_option_values` emits `("system_prompt_scope", "applies to every query on this node", False)` — a *label*, not the prompt text itself (the text is rendered once, via the generic prompt-key path, since `system_prompt` is now in `_ADVISOR_SUMMARY_VALUE_KEYS`/`_ADVISOR_SUMMARY_PROMPT_VALUE_KEYS`). Confirm there is no double-render: `_advisor_query_option_values` must never itself emit a `("system_prompt", <text>, True)` triple, only the `_scope` label.

**Before accepting Step 3 as closing Q A in full, verify one fact the review flagged as an open gap (Information Gap 1): does `system_prompt` apply once per query in the multi-query executor, matching the "applies to every query on this node" wording?**

```bash
cd "$(git rev-parse --show-toplevel)" && sed -n '670,690p' src/elspeth/plugins/transforms/llm/transform.py
```

Expected: inside the multi-query per-query message-building block (the class with `query_specs: Sequence[QuerySpec]` at `transform.py:506`), `if self.system_prompt: messages.append(ChatMessage(role="system", content=self.system_prompt))` appears once per query execution (this was independently confirmed during this planning pass — see the plan's own working notes; re-confirm at implementation time in case the plugin changes). This closes Information Gap 1 from the review: the label is accurate.

- [ ] **Step 4: Confirm the cap's withheld-count accounting is honest about node-template fallback beyond the cap**

```bash
cd "$(git rev-parse --show-toplevel)" && sed -n '9573,9580p' src/elspeth/web/composer/service.py
```

Expected: queries beyond `_ADVISOR_SUMMARY_MAX_QUERY_TEMPLATES` are still walked to check `entry.get("template") is None` and added to `node_template_users`, so `prompt_template_in_use` stays truthful even for queries whose own template text is withheld. This is the one place a rendering cap could silently create a *second* dishonest label if it were missed — confirm it is present.

- [ ] **Step 5: No commit needed** — verification-only, per Task 1.

---

### Task 3: Verify the injection pre-scan closes the security gap, not just the visibility gap

**Files:**
- Verify: `src/elspeth/web/composer/service.py:9258-9280` (`_advisor_prompt_option_values`)
- Test: `tests/unit/web/composer/test_advisor_checkpoint.py` (new tests, see below)

**Interfaces:**
- Consumes: `_advisor_query_option_values` (Task 2).
- Produces: `_advisor_prompt_option_values` now yields `queries.<name>.template` and `system_prompt` triples for the deterministic pre-scan (`_advisor_prompt_template_injection_finding`), not just for the renderer.

This is the review's most severe finding: before this diff, `queries` and `system_prompt` were invisible to **both** the renderer AND the deterministic injection pre-scan — an injection payload smuggled into a query template would reach neither the judge's eyes nor the fixed pre-scan gate. Verify this is now closed, and verify it with the strongest test the review recommended: a positive-control mutation, not just a structural code read.

- [ ] **Step 1: Confirm the scan wiring**

```bash
cd "$(git rev-parse --show-toplevel)" && sed -n '9270,9282p' src/elspeth/web/composer/service.py
```

Expected: `elif key == "queries": values.extend(_advisor_query_option_values(options))` inside `_advisor_prompt_option_values`'s per-key loop.

- [ ] **Step 2: Run the existing positive-control injection tests**

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && python -m pytest tests/unit/web/composer/test_advisor_checkpoint.py -k "injection_preflight" -n 0 -v > "$log" 2>&1; echo "exit=$?"; tail -60 "$log"
```

(`$log` is a unique, lane-private log path per AGENTS.md § Test Verification Policy — not a shared generic filename.)

Expected: `exit=0`. This exercises `test_advisor_injection_preflight_scans_query_templates_and_system_prompt` (parametrized: injection in a query template mapping form, in `system_prompt`, and in the list-authoring form of `queries`) and `test_advisor_injection_preflight_is_clean_on_the_incident_prompts` (negative control: the real incident prompts, which contain no injection text, must NOT flag). Per this repo's evidence doctrine (AGENTS.md § Claims Must Be Measured), a clean run on the negative control alone proves nothing — the parametrized positive-control cases in the same file are what prove the scan actually fires; confirm the log shows all parametrize IDs, not just the negative control, passing.

- [ ] **Step 3: Run the disagreement test**

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && python -m pytest tests/unit/web/composer/test_advisor_checkpoint.py -k "test_advisor_scan_and_render_agree_on_the_multi_query_prompt_surface" -n 0 -v
```

Expected: PASS. This is the render-set == scan-set pin, mirroring the existing `_advisor_control_flow_fields` disagreement test at `test_advisor_checkpoint.py:4579-4598` — the exact pattern the review recommended reusing rather than inventing a new one.

- [ ] **Step 4: No commit needed for this task alone** — folds into Task 5's full-suite run and commit.

---

### Task 4: Verify the rubric wording fix (Q D)

**Files:**
- Verify: `src/elspeth/web/composer/service.py:7908-7920` (the END checkpoint's `problem_summary` text)
- Test: `tests/unit/web/composer/test_advisor_checkpoint.py::test_end_checkpoint_problem_summary_carries_degeneracy_rubric`

**Interfaces:**
- Consumes: nothing new.
- Produces: the degeneracy-check sentence no longer hardcodes `prompt_template` by name.

The review's Q D finding: the FLAG in session 94f6f00c was the rubric executing *correctly* on incomplete evidence (the general mismatch-check sentence needs no change), but the degeneracy-check sentence specifically named `prompt_template`, so even after Tasks 1-3 land, a judge following that sentence literally could conclude the degeneracy check does not apply to a node whose `prompt_template` is now labelled "not used."

- [ ] **Step 1: Confirm the exact wording change**

```bash
cd "$(git rev-parse --show-toplevel)" && sed -n '7905,7922p' src/elspeth/web/composer/service.py
```

Expected: `"Use each LLM node's visible effective prompt text (its prompt_template, or in multi-query mode each queries.<name>.template plus the shared system_prompt; a prompt_template_in_use marker says which of those the node-level template still serves) and its listed, length-independent interpolated row fields to check one concrete degeneracy: …"`. Confirm this reads as scope-neutral (it no longer tells the judge to look at one specific key by name for the degeneracy check) and that it correctly cross-references the new `prompt_template_in_use` marker from Task 2 so the judge knows how to resolve which template is live.

- [ ] **Step 2: Confirm the general mismatch-check sentence is unchanged**

Confirm the sentence beginning "quote each explicit configuration constraint visible in the user's request excerpt … FLAG any visible mismatch" is byte-identical to before this diff (the review's Q D conclusion was this sentence needs no change — verify the diff did not touch it, which would be an unnecessary/riskier edit than the plan calls for).

- [ ] **Step 3: Run the rubric test**

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && python -m pytest tests/unit/web/composer/test_advisor_checkpoint.py -k "test_end_checkpoint_problem_summary_carries_degeneracy_rubric" -n 0 -v
```

Expected: PASS, asserting `"visible effective prompt text"` is present in the END summary and absent from the EARLY summary (the EARLY checkpoint's problem_summary is a different, shorter template that never had the degeneracy sentence — confirm the test's early/end split still makes sense).

---

### Task 5: Verify the row-field union fix (Q E's most important test) and run the full targeted suite

**Files:**
- Verify: `src/elspeth/web/composer/service.py:9599-9633` (`_node_effective_prompt_templates`), `:9995-10018` (`_render_interpolated_row_fields`)
- Test: `tests/unit/web/composer/test_advisor_checkpoint.py::test_summarize_interpolated_row_fields_reads_effective_query_templates`

**Interfaces:**
- Consumes: `_node_prompt_template`, `_well_formed_query_entries`, `_interpolated_row_fields` (all pre-existing).
- Produces: `_render_interpolated_row_fields` now unions `_interpolated_row_fields` over `_node_effective_prompt_templates(node)` instead of reading `_node_prompt_template(node)` alone.

This is the fix for the specific failure mode the review called out in Q E: the incident's own fixture has the dead node-level template and the live query templates coincidentally interpolating the *same* field (`row.colour`), so a fixture built only from the incident replay would pass even if the code still read the dead slot. The review required a second, divergent fixture to actually prove the union logic. Confirm that fixture exists and both directions are tested.

- [ ] **Step 1: Confirm `_node_effective_prompt_templates` excludes a fully-dead node-level template**

```bash
cd "$(git rev-parse --show-toplevel)" && sed -n '9599,9633p' src/elspeth/web/composer/service.py
```

Expected: the docstring states "A node-level template no query falls back to is dead and is NOT included" and the code's `elif override is None and node_template is not None: templates.append(node_template)` branch only includes the node-level template for queries that actually fall back to it.

- [ ] **Step 2: Confirm the divergent fixture test covers both directions**

```bash
cd "$(git rev-parse --show-toplevel)" && sed -n '/def test_summarize_interpolated_row_fields_reads_effective_query_templates/,/^def /p' tests/unit/web/composer/test_advisor_checkpoint.py | head -20
```

Expected two assertions, not one:
1. Dead node-level template interpolates a row field, live query templates interpolate nothing → signal must read `NONE` (this is the actual degeneracy the fix is supposed to catch — a false negative here is the whole point of F1's "degeneracy signal also reads only the dead prompt_template" sub-finding).
2. Dead node-level template interpolates nothing, live query templates interpolate a field → signal must read the live field (`[colour]`), not `NONE`.

If only one direction is present, this is a **gap, not a pass** — add the missing direction using the pattern in Step 3 below before proceeding.

- [ ] **Step 3 (only if Step 2 finds a gap): add the missing fixture direction**

```python
def test_summarize_interpolated_row_fields_reads_effective_query_templates(simple_state):
    """The degeneracy signal is computed over the templates that render, not the dead slot."""
    from elspeth.web.composer.service import _summarize_pipeline_for_advisor

    queries = {
        "good_pair": {"input_fields": {"colour": "colour"}, "template": "Name any colour pair."},
        "hex_code": {"input_fields": {"colour": "colour"}, "template": "Name any hex code."},
    }
    summary = _summarize_pipeline_for_advisor(simple_state.with_node(_multi_query_llm_node(queries=queries)))
    assert "interpolates row fields: NONE" in _node_line(summary, "colour_questions")

    summary = _summarize_pipeline_for_advisor(
        simple_state.with_node(_multi_query_llm_node(prompt_template="Answer in one short reply."))
    )
    assert "interpolates row fields: [colour]" in _node_line(summary, "colour_questions")
```

(This is already present verbatim in the working-tree diff as of this plan's writing — Step 2 should find no gap. Included here only as the exact fallback content if Step 2's verification fails.)

- [ ] **Step 4: Run the full advisor-checkpoint test file**

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && python -m pytest tests/unit/web/composer/test_advisor_checkpoint.py -n 0 > "$log" 2>&1; echo "exit=$?"; tail -40 "$log"
```

(`$log` is a unique, lane-private log path per AGENTS.md § Test Verification Policy.)

Expected: `exit=0`, all tests pass including every new test added by this diff and every pre-existing test in the file (a passing new test suite alongside a broken old one is not a clean landing).

- [ ] **Step 5: Run the composer unit suite scoped one level up, in case a sibling file imports the changed symbols**

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && python -m pytest tests/unit/web/composer/ -n 12 > "$log" 2>&1; echo "exit=$?"; tail -40 "$log"
```

(`$log` is a unique, lane-private log path per AGENTS.md § Test Verification Policy.)

Expected: `exit=0`. Per AGENTS.md, a scoped green run does not clear the whole-tree gates — run the full `pytest tests/` (see Task 7) before this lands anywhere near a merge, but this scoped run is the fast local signal for this task.

- [ ] **Step 6: Do NOT commit yet** — Task 6 below is new code that belongs in the same logical change (it touches the same evidence-budget concern this fix introduces more of). Commit once after Task 6, not twice.

---

### Task 6: Close the one gap the existing diff does not address — enforce or correctly document the `schema_excerpt` size bound

**Files:**
- Modify: `src/elspeth/web/composer/service.py` (two candidate sites below — pick ONE approach, see Step 1)
- Test: `tests/unit/web/composer/test_advisor_checkpoint.py` (new test)

**Interfaces:**
- Consumes: `self._settings.composer_advisor_max_prompt_tokens` (already a config field, `config.py:397`, default 4000), `_summarize_pipeline_for_advisor(state) -> str`.
- Produces: either an enforced ceiling on the checkpoint's `schema_excerpt`, or a corrected code comment that stops claiming an enforcement that does not apply to this path.

**Why this is a real, separate gap (verified during this planning pass, not carried over from the review unverified):** the code comment at `service.py:9488-9489` says the 1000-char prompt budget is "Kept well under the per-call char_cap (`composer_advisor_max_prompt_tokens * 4`) enforced in `_validate_advisor_arguments`." That enforcement function (`service.py:7299-7305`) is real, but it is only reachable from `_call_advisor_for_tool` (`service.py:8246`), which serves the `request_advisor_hint` tool. The EARLY/END checkpoint path builds `arguments` via `_build_checkpoint_arguments` and calls `_call_advisor_with_audit` **directly** (`service.py:8262-8268`), never passing through `_validate_advisor_arguments`. Confirmed by reading both call sites directly in this session — there is no size check between `_summarize_pipeline_for_advisor` and the provider call for the checkpoint path. With this diff's own new per-node budget (up to 8 queries × 1000 chars + a 1000-char `system_prompt` ≈ 9000 chars from ONE multi-query node, before that node's structural fields, control-flow fields, and every other node/source/sink in the pipeline), a pipeline with several near-cap multi-query LLM nodes has no backstop before the request reaches the provider.

- [ ] **Step 1: Decide the approach — this is a judgment call, not a mechanical fix. Surface it, don't decide unilaterally.**

Two honest options; do not silently pick one without flagging the tradeoff (per this repo's delivery posture: "If removing a practice is a marginal call or may discard a real safeguard, surface the tradeoff to the developer before removing it" applies in spirit here too, to *adding* one):

- **(a) Add enforcement**, mirroring `_validate_advisor_arguments`'s cap but applied to `pipeline_summary` before it is placed in `end_arguments`/`early_arguments`. Simplest form: truncate `_summarize_pipeline_for_advisor`'s output as a whole with an honest trailing marker (`"\n[…pipeline evidence truncated at N chars; contact the operator if per-node evidence caps need raising…]"`) rather than letting the provider call fail or the SDK truncate silently. This adds a cost/latency backstop at the expense of a small chance of losing evidence for a very large pipeline (rare in practice, but real).
- **(b) Correct the comment only**, if the maintainer judges per-field caps (120 chars/plain value, 1000 chars/prompt value, 8 queries/node, 8 fields/schema) are sufficient defense in depth on their own and an unenforced total is an acceptable, understood tradeoff for a composer session that is not adversarial in this dimension (the untrusted content here is the *values*, already scanned for injection; an oversized-but-honest evidence block is a cost/latency problem, not a correctness or security one).

This plan recommends **(a)**, because the comment currently asserts a false safety property, and AGENTS.md's evidence doctrine treats a claim of enforcement that isn't real as exactly the kind of "computed-then-discarded" pattern this project tracks as a defect class. But this is the maintainer's or team lead's call, not this plan's — **stop and get a decision here before writing code**, since (a) changes runtime behavior (evidence can now be truncated for a large pipeline) and (b) is a comment-only, zero-risk change. If no decision is available, do (b) first (it is strictly safe and takes five minutes), file the (a)-vs-stay-as-is question as a tracker issue, and stop.

- [ ] **Step 2a (if (a) is chosen): write the failing test first**

```python
def test_checkpoint_pipeline_summary_is_bounded_for_a_pathological_pipeline(simple_state, monkeypatch):
    """A pipeline with many near-cap multi-query LLM nodes must not produce an
    unbounded schema_excerpt — the checkpoint path has no size check between
    _summarize_pipeline_for_advisor and the provider call today (verified:
    service.py:8262-8268 calls _call_advisor_with_audit directly, bypassing
    _validate_advisor_arguments's char_cap, which is only reachable from the
    request_advisor_hint tool path)."""
    from elspeth.web.composer.service import _summarize_pipeline_for_advisor

    state = simple_state
    for i in range(50):
        queries = {
            f"q{n:02d}": {"input_fields": {"colour": "colour"}, "template": "x" * 900}
            for n in range(8)
        }
        state = state.with_node(_multi_query_llm_node(node_id=f"node_{i}", queries=queries))

    summary = _summarize_pipeline_for_advisor(state)
    # Exact bound depends on the Step-1(a) implementation chosen; assert
    # against whatever constant that implementation introduces, not a magic
    # number restated here.
    assert len(summary) <= _CHECKPOINT_SCHEMA_EXCERPT_MAX_CHARS
```

Run it and confirm it fails (no such constant exists yet):

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && python -m pytest tests/unit/web/composer/test_advisor_checkpoint.py -k "test_checkpoint_pipeline_summary_is_bounded" -n 0 -v
```

Expected: FAIL with a `NameError` or `ImportError` on the not-yet-defined bound constant.

- [ ] **Step 3a: implement the bound**

Add a module-level constant near the other `_ADVISOR_SUMMARY_*` constants (`service.py`, near line 9497) and apply it at the end of `_summarize_pipeline_for_advisor` (`service.py`, its `return "\n".join(lines)` line):

```python
# Total bound on the checkpoint's rendered pipeline evidence. The per-field
# budgets above (120 chars/plain value, 1000 chars/prompt value, 8
# queries/node, 8 schema fields) are defense in depth per-node, but nothing
# previously bounded their SUM across a whole pipeline — the checkpoint path
# calls _call_advisor_with_audit directly (service.py _build_checkpoint_arguments
# callers) and never passes through _validate_advisor_arguments's char_cap,
# which is reachable only from the request_advisor_hint tool. Truncate with an
# honest trailing marker rather than let an unusually large pipeline reach the
# provider unbounded.
_CHECKPOINT_SCHEMA_EXCERPT_MAX_CHARS: Final[int] = 48_000
_CHECKPOINT_SCHEMA_EXCERPT_TRUNCATED_MARKER: Final[str] = (
    "\n[…pipeline evidence truncated at {limit} chars; nodes beyond this point "
    "are not visible to this advisor pass…]"
)
```

```python
    rendered = "\n".join(lines)
    if len(rendered) > _CHECKPOINT_SCHEMA_EXCERPT_MAX_CHARS:
        marker = _CHECKPOINT_SCHEMA_EXCERPT_TRUNCATED_MARKER.format(limit=_CHECKPOINT_SCHEMA_EXCERPT_MAX_CHARS)
        rendered = rendered[: _CHECKPOINT_SCHEMA_EXCERPT_MAX_CHARS - len(marker)] + marker
    return rendered
```

Pick the exact constant value with the team lead/maintainer — `48_000` here is a placeholder sized to comfortably fit several near-cap multi-query nodes while staying well under typical provider context limits; it is not derived from a measured requirement and must not be treated as validated until someone confirms it against the actual `composer_advisor_model`'s context window and the `composer_advisor_max_prompt_tokens` setting's intent.

- [ ] **Step 4a: run the test again, confirm it passes**

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && python -m pytest tests/unit/web/composer/test_advisor_checkpoint.py -k "test_checkpoint_pipeline_summary_is_bounded" -n 0 -v
```

Expected: PASS.

- [ ] **Step 2b (if (b) is chosen instead): fix the comment only**

At `service.py:9488-9489`, replace the claim of enforcement with an accurate statement:

```python
# Prompt-shaped option values (``prompt_template``/``template``/``system_prompt``)
# get a much larger render budget so the advisor sees the WHOLE prompt — its
# rubric anchors and (for the degeneracy check) its row-field interpolations —
# not just the opening line. This per-value budget, the 8-query-per-node cap,
# and the 8-field schema cap are per-node defense in depth; there is currently
# no enforced ceiling on the SUM across a whole pipeline's schema_excerpt for
# the EARLY/END checkpoint path specifically (unlike the request_advisor_hint
# tool, whose arguments ARE bounded by _validate_advisor_arguments's char_cap).
# If this ever needs a hard total bound, add it at the end of
# _summarize_pipeline_for_advisor, not here.
```

No test needed for (b) — it is a comment-only change. Still run the affected file's test suite once to confirm no accidental edit slipped in beside the comment.

- [ ] **Step 5: Commit**

```bash
cd "$(git rev-parse --show-toplevel)" && scripts/branch-safety-check.sh --intent commit
```

Read every `[FAIL]` line before proceeding; a `[WARN]` is not blocking but should be understood.

```bash
cd "$(git rev-parse --show-toplevel)" && git status --short
```

Confirm the staged set is exactly `src/elspeth/web/composer/service.py` and `tests/unit/web/composer/test_advisor_checkpoint.py` — nothing under `.claude/lanes/`, no scratch logs, no the unrelated untracked `docs/plans/2026-09-13-kubernetes-and-identity-master-plan.md`.

```bash
cd "$(git rev-parse --show-toplevel)" && git commit -- src/elspeth/web/composer/service.py tests/unit/web/composer/test_advisor_checkpoint.py -m "$(cat <<'EOF'
fix(composer): admit multi-query LLM prompts to advisor evidence

The END-gate advisor could see only a multi-query LLM node's dead
node-level prompt_template, never the per-query queries.<name>.template
overrides or the shared system_prompt that actually run (session
94f6f00c). This closes both the evidence gap (the judge FLAGged a
constraint the live prompts already met and could not see the repair)
and a parallel injection-scan gap (the same two fields were unscanned
by the deterministic pre-scan, not just unrendered).

EOF
)"
```

(Adjust the message if Task 6 lands in the same commit — mention the `schema_excerpt` bound explicitly if approach (a) was chosen.)

---

### Task 7: Full-suite gate before this is mergeable

**Files:** none (verification task).

- [ ] **Step 1: Run the canonical full-suite gate, detached**

```bash
cd "$(git rev-parse --show-toplevel)" && scripts/full-suite-gate.sh --execute --detach
```

- [ ] **Step 2: Poll for completion and read `summary.txt`, not terminal output**

Follow the printed `.done` path per the script's own instructions; do not `tail` the running suite (AGENTS.md: "The suite is never piped through `tail`").

- [ ] **Step 3: Compare the trust-tier / static-analysis finding corpus before and after this change, not to zero**

Per AGENTS.md's Judge-signature stage section, the trust-tier gate is a deliberate fail-closed state independent of this fix; do not attempt to clear it globally. Confirm this change does not add a NEW finding by diffing the corpus, and stop there.

- [ ] **Step 4: If schema/SQL/session/Landscape persistence was touched (it was not, by this plan's scope) also run the testcontainer suite.** Not applicable here — no schema, SQL, session, or lock code is touched by Tasks 1-6. Skip.

---

## Self-Review

**Spec coverage against the SME review's five questions plus its two extra findings:**

| Review item | Task | Status per this planning pass |
|---|---|---|
| Q A — `system_prompt` scope labelling | Task 2, Step 3 | Implemented in working tree; verified wording and the multi-query call-site claim |
| Q B — dead `prompt_template` label wording | Task 2, Step 2 | Implemented in working tree; wording matches the review's fact-register recommendation |
| Q C — 8-query cap + withheld count | Task 2, Steps 1, 4 | Implemented in working tree; withheld-escape-hatch wording (a review nice-to-have, not a must-fix) NOT added — judged out of scope, see note below |
| Q D — rubric wording | Task 4 | Implemented in working tree; verified the general mismatch sentence was correctly left unchanged |
| Q E — deterministic measurement | Task 3 (security), Task 5 (row-field union) | Implemented in working tree, including the divergent-fixture test the review specifically required |
| Security gap (pre-scan blind to `queries`/`system_prompt`) | Task 3 | Implemented in working tree, with positive-control mutation tests and a disagreement pin |
| `schema_excerpt` total-size enforcement gap | Task 6 | **Not implemented anywhere** — genuinely new work, judgment call flagged for the maintainer |

**Placeholder scan:** no "TBD"/"handle appropriately" left in this plan; Task 6's two sub-options are both fully specified with real code, and the decision point between them is an explicit judgment call surfaced to the maintainer, not a placeholder.

**Type/name consistency:** `_advisor_query_option_values`, `_node_effective_prompt_templates`, `_ADVISOR_SUMMARY_MAX_QUERY_TEMPLATES`, `_CHECKPOINT_SCHEMA_EXCERPT_MAX_CHARS` are used consistently between the task that introduces or verifies them and every later reference.

**Explicitly out of scope for this plan** (noted, not silently dropped): the review's suggestion to reference an escape-hatch tool in the `additional_queries_withheld` message wording (Q C) — the existing `_render_schema_for_advisor` precedent this diff correctly mirrors does not do this for schema fields either, so adding it only for queries would be an inconsistent embellishment beyond what the review's own precedent argument supports. Leave as-is unless the maintainer asks for it separately.
