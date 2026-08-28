# Tier burn-down — Wave 2 handover (2026-08-29, hub session "justify-burndown")

> **WAVE 2 CLOSED 2026-08-29 at `feature/unified-lineage@9814aa794`.** Every outstanding item
> below is DONE: B43c merged (0fa1f5d5c), lens A done + dispositioned (comments 8798/8801),
> lensA-wording merged + symlink adjudication (2b6e87bc4), full suite green at 2b6e87bc4
> (43,455 passed; the 2 failures were pre-wave — Codex-plan staging token + unpinned
> f0e38838d policy-hash rotation — fixed 9814aa794 and re-run green), all 28 bucket issues
> closed with commit anchor 9814aa794, observations swept (6 dismissed into the ledger).
> **PAUSED before Wave 3 per John's instruction.** Sections below are preserved for the
> re-stage seam and Wave-3 briefs.


Plan: `docs/superpowers/plans/2026-08-28-tier-model-justify-burndown.md` (+ `.buckets.json`).
Epic: `elspeth-3ab6107b1f`. Branch: `feature/unified-lineage`. Wave-2 base `2ca7d2227` → tip **`1ef98bcba`**.
Wave-1 handover (still valid for its content): `2026-08-28-tier-burndown-wave1-handover.md`.

## State at handover — WAVE 2 CODE-COMPLETE, CLOSE-OUT IN FLIGHT

All 28 Wave-2 buckets merged `--no-ff` (B24–B55; `git log --first-parent 2ca7d2227..1ef98bcba`),
plus follow-ups B53b (restore 2 rationales), B39b/B39c (deep_freeze pins), B43b (tool_batch
stale-signed sites), B40b (frozen-state pins). ~618 findings removed by code, ~365 rationalised
(sidecars `docs/agents/sweeps/tier-burndown/B{24..55}.rationales.json`). A Codex session also
landed 3 unrelated `docs(plans)` commits on the branch (`2ab533425`, `ed1592ee1`, `7c367a916`) — not ours.

**OUTSTANDING before the wave can close (in order):**
1. **B43c fix (P1 regression, IN FLIGHT).** B43b introduced `type(candidate_data) is dict` in
   `tool_batch.run_tool_batch`'s prevalidation-rejection arm; `ToolResult.__post_init__` freezes
   `data` → mappingproxy → guard always False → PREVALIDATION_REJECTED feedback payload to the
   planner changed shape (error/error_code buried under `candidate_data`). Lane tier-B43 was
   resumed after a session-limit cut; worktree `.claude/worktrees/tier-B43c` holds its
   UNCOMMITTED fix (tool_batch.py + test_freeform_proposal_prevalidation.py + sidecar + hints).
   Requirements given: Mapping-compatible guard restored, frozen-input test through the REAL
   ToolResult producer, both arms, mutation-checked; re-verify its other `type() is` conversions.
   If the lane is gone: finish from the worktree (ping first — pytest may be running).
2. **Lens A: DONE (epic comment 8798, 2026-08-28). ALL-STOP: none.** Covered all 38 merges via
   9 file-owning sub-lanes + a range-wide sweep, INCLUDING the composer-invariant half lens B
   left unswept (Class 3: zero findings — no server authoring, no tutorial-special path, every
   new boundary's suppression ≤ its honest parse). Range-wide frozen-narrowing sweep: B43b's
   tool_batch site is the ONLY `type() is` read on a freeze_fields'd field in the range — the
   ledger's "re-run frozen-narrowing audit" wave-end mandate is SATISFIED. Hub disposition of
   its findings (all landed 2026-08-28/29):
   - F3 FIXED `6a22c6087` (required_controls `or "discard"` coerced falsy non-str; now
     absent/null-only default, falsy-value test mutation-checked).
   - F6+F7 FIXED `6f2aae0c9` (false ToolResult-never-subclassed comment in tools/sessions.py;
     false impostor-`__contains__` rationale in recent-code-hints.md).
   - F2 → bug `elspeth-3db5745ba7` (P1, pre-existing: blobs active-run retention guard reads the
     dead pre-2026-05 column shape; TypeError 500 on every delete/replace under an active run;
     pinning tests seed the dead shape).
   - F4 config rider → bug `elspeth-4562900623` (P3, `_is_loopback_or_private_origin` accepts
     `127.1`/hex/decimal/octal/localhost.localdomain as public_base_url).
   - F9a/b/c + unpinned narrowings → task `elspeth-7b5af9015b` (P3 residual worklist).
   - F4/F5 signed-sidecar wording defects (~13 items across B25/B34/B33/B24/B35/B47) + B32
     docstring → fix lane `tier/lensA-wording` (worktree `.claude/worktrees/lensA-wording`);
     merge before the suite. F8 (B39 signing bookkeeping) already in the hub ledger.
   - Wave-3 method notes from the audit: the repo's `elspeth-xdist-auto` plugin defaults pytest
     to `-n auto` — EVERY lane/sub-lane brief must mandate an explicit `-n`; loomweave
     under-enumerates callers vs `git grep` (missed 3/6 redaction callers) — grep is the
     authority for reachability claims.
3. **Full suite** on the final tip (after B43c): worktree + `-n 16` background job, per
   `feedback_long_suite_runs_use_a_worktree_not_the_shared_checkout`. Expected caveats: same as
   Wave 1 (litellm-poisoning fixtures; 3 pre-existing freeze_guards) + `test_validation_trust_tier.py`
   declaration pins were updated by B53.
4. **Close the Filigree issues** (28 bucket tasks, all at `in_progress` under the epic, map in
   `scratchpad/wave2-issues.txt`) with commit anchor = final tip. Sweep `observation_list` for
   all-stop candidates (none so far).
5. **PAUSE. Do not start Wave 3 (sessions + plugin_policy: B54, B56, B26–28 acceptance) without
   John's go.**

## John's rulings this wave (BINDING for Wave 3 briefs)
- **Counting rule for burn-down reporting:** the metric is RETIRED RISK, measured as the
  raw-corpus finding delta (allowlist disabled, identical command at each anchor). A
  boundary conversion (finding → `R_TB_SUPPRESSED` with a `test_ref`) counts as a removal the
  same as restructuring — "adding tests retires the risk in the same way that removing the
  code does." Rationalised sidecar sites are NOT removals (still findings in a raw scan;
  documented risk awaiting the operator). Measured W1+W2: 3,898 → 2,932 = **966 removed**
  (345 + 621; ~324 via boundary+test, ~642 restructured), ~552 rationalised carried.
  Saved corpora: scratchpad `raw_tier.txt` / `raw_tier_w2.txt` / `raw_tier_tip_postB43c.txt`.
- **"An honest fix is always preferred to minimising churn; signing effort is the lowest priority
  compared to clean and honest code."** (4th statement; memory
  `feedback_never_shape_code_around_signature_churn` updated.) Lanes may fix signed sites when the
  right change removes the finding (entry → stale_delete); the ONLY exclusion is a rationale that
  restates a still-binding signed ruling for unchanged code. Never withhold a fix to protect a
  signature; "two authorities per site" is a hub reconciliation problem, not a reason to withhold.
- Pause at end of each wave; ping only for all-stops.

## The Wave-2 defect class (put at the TOP of Wave-3 briefs)
**freeze-after-construction vs exact-type narrowing.** `NodeSpec/SourceSpec.__post_init__` freeze
`options` AND `branches`; `ToolResult.__post_init__` freezes `data`; `deep_freeze` renders nested
mappings `mappingproxy` and lists `tuple`. Therefore `type(x) is dict/list` on any value read off a
frozen owner is ALWAYS False. Rules distilled (all measured):
- Check the CONTAINER that holds the field, not the producers (B43b's error).
- POLARITY first: reject-gate (`is not dict → raise`) fails closed; accept-gate (`→ return/skip`)
  is silently disabled — tests stay green (8838 passed over B43b's).
- The discriminating question is what stands between the frozen owner and the read: a thaw
  (`deep_thaw`, `json.loads`, pydantic re-materialisation at `model_validate`) → safe; the pair
  names deep_freeze's output (`(dict, MappingProxyType)`, `(list, tuple)`) → safe; nothing → trap.
- A frozen-input pin must construct through the REAL producer (never `deep_freeze({...})` on a
  literal), use the MAPPED branches form, and assert BOTH arms (B39c/B50 measured; single-arm and
  literal-fixture versions stay green under the mutation they exist to catch).
- Audit of all ~53 wave-introduced `type() is` sites: 0 defects besides B43b (epic comments 8784 +
  8797); two SAFE-only-by-provenance sites got pins in B40b; hygiene items in hub notes.
- `isinstance(x, Enum)` and stdlib/ast unions can never convert; `type() is C` gives NO negative-arm
  mypy narrowing on unions (B41/B45 measured) — else-branch users stay isinstance.

## Lint/tooling facts learned (for the lint-precision ticket elspeth-8d46db34ff and Wave-3 briefs)
- R4→R6 conversion: narrowing a broad except does NOT clear R4 — it becomes R6 at the same line;
  only a non-default-returning handler clears it (B43b, in hints).
- R4 fires on explicit-`return` probe handlers; R6 fires on accumulator-append handlers — both
  rationalise, never re-raise (B25/B34; 3rd+4th precision defects on the ticket).
- Boundary suppression: walk loses the trail through `enumerate()` CALLS, helper return values,
  names bound in a `try:` whose handler returns, closures (per-scope), `vars()`; `mapping.items()`
  keeps it. Confirm from `R_TB_SUPPRESSED` lines, never by reading the decorator. Widening an
  existing decorator's `suppresses=` tuple is zero-code removal (B47: 20 findings; B42: 9).
- A boundary-suppressed site stops counting toward per-file `max_hits` ceilings (B40, measured).
- Signed entries bind by `scope_fingerprint`, NOT `fp=`/ast_path (B45) — drift ≠ unbound; only
  scope_fingerprint mismatch reports `Stale`. The "Refusing to load" warning is per-ENTRY and only
  for deleted files (B33 proved from `core/allowlist.py`; B42's contrary claim retracted).
- Hand-computed sidecar keys: visitor `file_path` must be scan-root-relative (`src/elspeth`) or it
  INVENTS findings (B33). Sidecar `ast=` from the POST-edit tree via the rule's own `scan_file`.
- Worktree hooks: mypy resolves `elspeth` to the MAIN checkout without PYTHONPATH; merge tip FIRST,
  then export (B44+B47). Verify the worktree still exists before any `cd` sequence (B43).
- Wardline: 17 active PY-WL-102 are ALL false positives (pack lacks a non-raising-validator
  marker) — ticket `elspeth-2a4f2fd48b`; run the CLI (`/home/john/.local/bin/wardline`) from inside
  the worktree, MCP scans the main checkout.

## Re-stage ledger (operator seam; ONE stage_scan after the LAST fix merge)
Reconciliation rule for stale-signed sites: sidecar rationale WINS → stale fp entry = stale_delete;
no sidecar + code unchanged → drift_repair keeps the ruling; finding gone → stale_delete. Never two
records per site. Before ANY drift_repair, READ the old rationale prose against live code — B37
found 4 signed rationales that are factually WRONG (argue an absent-key branch an exact-key
assertion makes unreachable) → delete, don't re-derive (obs elspeth-obs-ca3fd469e9).
Known stale_delete/drift lists (details in `scratchpad/wave1-hub-notes.md` + lane comments):
steps.py allow_hits[154]; tool_batch ×3 (superseded by B43b sidecar); generation.py ×5 + missing
9th `_row_fields_referenced_by_condition` entry (obs elspeth-obs-07866fb4e4); execution/service.py
×7; composer/service.py ×10; chat_solver ×4 (wrong prose); B52's ×2; Wave-1 list unchanged.
Operator calls queued: tool_batch per-file R6 ratchet 5/3 (2 genuine new boundaries — update
reason+cap or commission extraction, obs elspeth-obs-8b5c2d89e5); _common.py R1 rule now Unused +
R5 ratchet 13→7 (obs elspeth-obs-e2ff0cc748); chat_solver R5 ratchet 31/11 (obs
elspeth-obs-872262ee44). Judge-quality gate: operator must re-run `check-judge-quality` (policy
hash changed at f0e38838d) before any sign-bundle. `elspeth-23ee8e3440` still owed before re-stage.

## Open tickets touched/created this wave
- `elspeth-2a4f2fd48b` (P2): wardline pack non-raising marker.
- `elspeth-61ae1eaa32` (P2 bug): run_status_projection.py:207 unhashable membership.
- `elspeth-8d46db34ff` (P2): now FOUR lint-precision defects (R4 return, R6 accumulator + the two
  Wave-1 ones) — after re-stage.
- Hygiene list (wave-end or Wave-3 hygiene lane) in `scratchpad/wave1-hub-notes.md`: provenance
  pins for `_discover_blob_rows_sources` + `normalize_set_pipeline_redacted_arguments`;
  knob_schema accept-gates fail-closed (obs elspeth-obs-cf02fe5c79); planner_authoring_aids
  `.get(plugin_id, ())` honest fix; `_canonical_state_from_private_pipeline` bare TypeError.
- Notable lane observations to sweep at close-out: elspeth-obs-a43ca0c1f5 (P2 prompt_template
  validates green in composer), elspeth-obs-afb990aaa4/46e82b26eb (B48 YAML gate ordering /
  on_validation_failure conflict), elspeth-obs-4125c6fea4 (planner AssertionError → raw 500),
  elspeth-obs-6a5ecc711c (sessions bind path swallow), elspeth-obs-055a2798bc (B41 sidecar clause —
  FIXED by hub at 2f7f3c17b), elspeth-obs-76c93267eb (P3 untested slog).

## Process facts for the resumed hub
- Hub is the ONLY writer to the shared checkout; lanes self-provision worktrees off the tip when
  theirs is reclaimed (B39c normalised this). Ping a lane before reclaiming; "idle" ≠ done (check
  branch commits + running pytest + Filigree comment).
- recent-code-hints.md merge conflicts: ALWAYS additive, keep both sides (script in transcript).
- interpretation_state.py re-export conflicts: keep HEAD (B47's canonical form).
- Measure before relaying any mechanism claim; when two lanes contradict, measure (memory
  `feedback_contradictory_lane_claims_measure_dont_relay` — three corrected broadcasts this wave).
- Session-limit deaths: resume agents by SendMessage to the same name (context intact); tell them
  to distrust orphaned background logs (summary line, not rc).

## Scratchpad artefacts (session-local; regenerate or copy before they vanish)
`LANE_BRIEF_W2.md` (15 items — the Wave-3 brief should be derived from it), `CONTROL_AUDIT_BRIEF_W2.md`,
`worklists2/B*.md`, `raw_tier_w2.txt`, `wave2-issues.txt`, `wave1-hub-notes.md` (the running ledger —
COPY THE RE-STAGE + HYGIENE + OPERATOR SECTIONS somewhere durable if the session dies), `empty-allowlist/`.
