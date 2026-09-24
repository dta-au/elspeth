# Composer strict tool contracts: S2 in small steps

- **Date:** 2026-09-25
- **Master plan:** `docs/plans/2026-09-23-composer-strict-tool-contracts.md` (§4 S2 is the design of record; this plan
  replaces its one-slice rollout with small steps).
- **Direction (John, 2026-09-25):** "include it in small steps, we'll clean up those surfaces one at a time".
- **Tree:** local `release/0.8.1` lineage at `1f500c6ae` (S0, S1, the branch-order fixes, session schema epoch 67).
  `$W` is the worktree root and `$L` is `$W/.claude/lanes/s2plan`, the gitignored lane that holds the evidence notes
  this plan draws on (`understand-wire.md`, `understand-surfaces.md`, `understand-measure.md`, `design-per-tool.md`,
  `design-per-surface.md`) and their instruments. The lane is lost when the worktree is removed.
- **Citations.** These were re-read at `1f500c6ae` for this plan: `tool_batch.py:494-501, 918, 1058-1092`;
  `tools/wire_projection.py:217, 494-508, 917-925, 933-945`; `protocol.py:904-906, 993-997, 1091, 1392-1393`;
  `bounded_json.py:116-152`; `pipeline_planner.py:1669-1678, 3794`; `contracts/composer_audit.py:79, 115-118`;
  `redaction.py:403-409, 1406-1412`; `turn_audit.py:53-66, 242-243`; `sessions/models.py:1683-1685`;
  `execution/_validation_diagnostics.py:135, 143, 146, 168, 376, 409`; `state.py:3574`; `tools/_common.py:1987-1991`;
  `skills/pipeline_composer.md:478, 1212`; `skills/pipeline_capabilities.md:179`;
  `planner_authoring_aids.py:2731, 2766-2779`; `prompts.py:258`; `capability_skill.py:248, 261`; and the partition
  pins in §3.5 item 10. Every other `file:line` comes from the lane notes, which cite their own controls. Lines drift; re-anchor
  with `grep -n` before editing. The plan review (§8) re-read further sites, cited where they are used.
- **Paths.** A bare file name resolves under `src/elspeth/web/composer/` (`protocol.py`, `state.py`, `prompts.py`,
  `service.py`, `audit.py` and `tool_batch.py` each have namesakes elsewhere under `src/elspeth/web/`; the composer copy is
  meant). `tools/…` and `skills/…` are under that directory too. `execution/…` and `sessions/…` resolve under
  `src/elspeth/web/`; `contracts/…` and `core/…` under `src/elspeth/`; tests under `tests/unit/web/composer/` unless a
  directory is given.
- **Sizes** (S, M, L, XL) are estimates, not measurements.

---

## 1. Header

### 1.1 Status

**R1 is yes, deferred** (John, 2026-09-25; master plan §1, §6.3). On 2026-09-24 John ruled R1 no and withdrew S2 and
S3. On 2026-09-25 he reversed that: "lets prepare this package on the assumption that the answer is yet (because it
has to be) because the answer is going to be no until its yes (i.e. we do want this, but the codebase is very
volatile right now because we just landed a dozen massive fixes across different subsystems)". S2 and S3 are
reinstated as planned work, done in the small steps below. This plan is the S2 rollout.

- **Start condition.** Codebase stability, which is John's call. It is not the data-driven reopen trigger of
  2026-09-24, which is no longer a precondition (§7.2, R-G). Two further preconditions come from the S1 live
  regression (§2, principle 9): the S1 zero-property fix has landed, and a live window shows the 22 remaining strict
  tools healthy.
- **Tier M, measurement (M1a, M1b, M2 to M6), and Tier A, wire-neutral surface cleanups (A1 to A5, A7, A8; A6 is
  withdrawn, §8), can proceed as soon as John says the codebase is stable enough.** Tier M is the instrument every
  flip's acceptance reads (master plan §5.4). M1a, M1b and M5 together close ticket `elspeth-a1864430a1`; they need
  rulings first (§7.2, R-I and R-J). Each Tier A step rewrites one teaching or diagnostic surface so it is true on both
  wire forms, or adds a gate. The wire bytes do not change. The text steps have their value in coming before the flip
  that would otherwise contradict them. A7 (the W-side teaching census) is a test gate over W, not production text; it
  carries one closed exemption (the `set_pipeline` envelope) and has teeth from F1. A8 fixes a live defect (a false
  "byte-identical arguments" hint after repeated wire-stage rejections) that exists today on `set_pipeline` and on
  over-bound JSON, so it can land at any time.
- **Tier P and Tier F proceed in the order of §3.1, without a per-tool reopen ruling.** Each flip needs its live
  canary (principle 9) before and after, and the Tier R rulings in §7.2 that gate it are still open. A flip that shows
  no benefit or a regression is reverted (principle 7, §5.3). The R2 pair codec (R2 was ruled yes) still lands inside
  the first flip that uses it (F4, `upsert_node`), never ahead of it: a pair decoder with no tool using it is
  machinery with no producer, which S0 refused for exactly this class (`wire_decode` was left out of the enum for that
  reason).
- **The 2026-09-24 evidence stays recorded** (master plan §1.2, §6.3): 30 historical option-tool failures, 10 shape, 18
  content and 2 ambiguous; `strict` on a carrier string constrains nothing inside it; options-as-string has swung twice
  on this seam (`a5d9e5414`, `e50604428`). It is context and per-step acceptance and measurement input (§5), not a
  gate.
- **Three steps cannot be made small.** Under R-F (no machinery ahead of its first producer), F1 (L), F4 (L) and F5
  (XL) are the irreducible large steps: each carries machinery whose only producer is that flip. Everything that can
  run today with identical output has been pulled out of them (P3, P4), and so has the live-defect fix A8. If John
  wants F1, F4 or F5 smaller still,
  the price is relaxing R-F for that machinery; §7.2 R-F puts the trade to him directly.

### 1.2 Goal

Where a route can enforce a grammar (`openai_strict`), send each option-bearing tool `strict: true`, one tool at a
time, without ever letting a tool accept two argument forms on the same route. Every teaching and diagnostic surface
is cleaned up one surface per step, before the flip that would otherwise contradict it.

### 1.3 What S2 does not do

- **It does not change S**, the semantic contract: the flat registry (`get_tool_definitions()`), the pydantic argument
  models, MCP `inputSchema`, the capability-core field tables. `test_tool_declarations.py` and Check 7
  (`test_none_w_is_s_plus_the_envelope`) never move. If one does, the step changed S (§7.3).
- **It does not change the `none` dialect.** Anthropic, Bedrock, custom gateways and `composer_strict_tools=off` keep
  today's bytes.
- **It does not change what audit, redaction, the frontend or MCP read.** All of them read S after decode. Two
  redaction projections do change: the ARG_ERROR tool-row result projection (M1b) and the redaction-model comment at
  `redaction.py:1406-1408` (F1).
- **No DDL and no epoch bump.** M1a, M1b, M2 and M3 add or change JSON keys only.
- **It does not reduce content errors.** `strict` on an `options_json` string constrains nothing inside the string.
  18 of the 30 historical option-tool failures were content errors (master plan §2.3); no step here reaches them.
- **It does not touch the pipeline-planner terminal** or the planner's shared argument parse
  (`pipeline_planner.py:1669-1678`, which runs on every planner tool call, the terminal included, `:1793`). That is S3
  (§6). P1 is scoped to the compose loop for this reason.
- **It does not author anything server-side.** Decode transcodes losslessly (R2) or rejects. It never inserts a
  value, picks a branch form or repairs a call. That keeps composer invariant 1. The tutorial gets no special path
  (invariant 2); it simply exercises none of these tools (§5.1).
- **Guided surfaces are out of scope** (guided mode is being removed).

### 1.4 Relation to S1 and S3

- **S1** shipped the machinery S2 extends: static per-dialect W, the decode plan (`EnvelopeUnwrap`, `StripNull`),
  `encode_semantic_arguments`, the ledger, `wire_conformant`, `strict_transport.py`, `strict_profile.py`, the
  `composer_strict_tools` setting and the repair signal. S1's owed dev deployment acceptance is a prerequisite for the
  baseline round (M6).
- **S1's live regression** (found 2026-09-25; master plan §4 S1): the 10 zero-property tools S1 sends `strict:true`
  fail on every call on the deployed route. Its fix is a precondition for this plan (§1.1, §2 principle 9).
- **S3** (the planner terminal) is reinstated with S2 and follows F5. It is independent of every step here as long as
  each step changes W only. §6 says what S3 needs after F5.

### 1.5 Which design this plan uses

Two designs were drafted in the lane: one organised by surface (`design-per-surface.md`) and one by tool
(`design-per-tool.md`). They agree on most things: measurement first, one wire-neutral surface per step, the D12
overturn as a precondition for any flip, machinery landing only with its first producer, and S3 staying orthogonal.
This plan takes:

- **from the per-surface design:** the organising rule for teaching (§4). `encode_semantic_arguments` is the single
  source of truth for how a position is spelled; a surface that shows a concrete argument renders it through
  `encode`, and every other surface names positions by semantic path only. It also takes the carrier-table design
  that makes un-flipping one tool a row deletion, the tri-state detector landing with the first nullable carrier, the
  semantic memo key for exemplars, and grouping battery rounds per tranche.
- **from the per-tool design:** the flip granularity (one tool per flip, so the programme can stop after any flip),
  the `patch_node_options` pilot, the more complete rulings list (notably R-H, the explicit reading of "no dual
  acceptance"), the honest sizing of the first flip, and two corrections it verified in this tree (§7.4).

The per-tool flip order is changed in one place: the other two `patch_*` tools flip straight after the pilot, so the
`patch`/`patch_json` split within one family lasts as short a time as possible. The per-tool design offered this as
its "family alternative".

---

## 2. Principles for every step

1. **One consistent, shippable state per step.** After every step, each tool on each route accepts exactly one
   argument form. The programme can stop after any step and leave a correct system.
2. **No dual acceptance, read per (tool, route).** A flipped tool on `openai_strict` accepts only its carrier form: the
   S-form key (`options`, `patch`, a map object) at a declared carrier or pair position is rejected as `wire_decode`,
   as `EnvelopeUnwrap` already rejects the unwrapped `set_pipeline` form (`wire_projection.py:917-925`). On `none` and
   MCP the same tool accepts only the object form, and a stray `options_json` fails S's closed root as `schema_shape`.
   This overturns S1 decision D12 and needs ruling R-A. No compatibility shims, no window where one route takes both.
3. **One surface or one tool per step.** A Tier A step changes one surface. A Tier F step flips one tool, with that
   tool's teaching, diagnostics and pins in the same step. Machinery lands with its first producer, never ahead of it
   unused (S0 precedent). Code that runs today with identical output (P2, P3, P4) is not "ahead of its producer"; a
   parameter or branch nothing calls yet is.
4. **Wire bytes are frozen outside Tier F.** Every Tier M, A and P step keeps the tool-list bytes identical on every
   dialect: a SHA pin of the loop list on both dialects, the planner lists and both palettes against the previous
   build, as S1 did.
5. **Full-suite gate before every merge.** `scripts/full-suite-gate.sh --execute --detach --stages
   ruff,mypy,contracts,lints,pytest`, run when the host has capacity. Add `testcontainer` for any step that touches
   session persistence (M1a, M1b, M2, M3). Whether one-string text steps may share a gate is an open decision (R-K).
6. **RED first**, targeted `-n 0` runs, the trust-tier corpus compared before and after (never to zero), no signatures
   staged, `scripts/check_contracts.py --write-census` if a new `Mapping[str, Any]` signature appears.
7. **The revert unit is one commit** (Tier M, A, P) **or one merge** (Tier F). A flip may be several reviewable commits
   under one merge, one gate and one paired round. For a flip, the immediate operational lever is
   `composer_strict_tools=off`. It is coarse, because it also drops S1's 32 strict tools, so it stops the bleeding but
   is not the revert. The carrier table is built so that un-flipping one earlier tool is a row deletion plus its
   teaching entries and pin moves, without reverting later flips. **Rollback order:** once a flip has landed, the
   Tier A and P steps it depends on are no longer independently revertible; revert the flip first. Each flip
   re-asserts, in its own test module, the neutral-text pins of the surfaces it depends on, so reverting an A step
   under a landed flip turns the flip's module red instead of passing silently.
8. **Pre-register each live gate** before its first round (§5.3).
9. **Every wire flip needs a live tool-calling canary on the deployed route.** For each schema shape the flip
   changes (zero-property, carrier string, pair array, nested), the canary makes real tool calls against the stamped
   list, measured per `provider_served` endpoint, before and after the flip. Offline byte proofs (the fidelity matrix,
   SHA pins, the boot probe) are necessary but not sufficient. This comes from S1's live regression (found 2026-09-25,
   session `ed3c015b-a2f7-4322-9f39-1716541c5796`; master plan §4 S1): the 10 zero-property tools S1 sends `strict:true`
   (`list_blobs`, `list_composer_blobs`, `list_sources`, `get_expression_grammar`, `get_audit_info`,
   `preview_pipeline`, `diff_pipeline`, `list_transforms`, `list_sinks`, `list_secret_refs`) fail on every call on the
   deployed route (`openrouter/deepseek/deepseek-v4.1-flash`, served by Together). `preview_pipeline` went from 36 of 36
   OK before S1 to 0 of 9 after it, and `list_blobs` from 19 of 19 to 0 of 1. Each rejected call carries exactly one
   stray key (`field_count` 1), and the planner looped 10 times on the bare "got invalid_schema" because S1's repair
   signal was absent. S1's offline gates (the loopback fidelity matrix, and a 16-token boot probe that makes no tool
   call) could not see it.
   **S2 does not start until that regression's fix has landed** (defect 0 in
   `docs/plans/2026-09-25-composer-live-run-defects-fix-prompt.md`, in the main checkout and untracked when this was
   written: stamp the zero-property tools `strict:false`, partition 32/10 → 22/20) **and a live window shows the 22
   remaining strict tools healthy.** Until then the mitigation is `ELSPETH_WEB__COMPOSER_STRICT_TOOLS=off`.

---

## 3. The steps

### 3.1 Order at a glance

| # | Step | Tier | Size | Model-visible | Wire flip (needs the live canary, principle 9) |
|---|---|---|---|---|---|
| 1 | M1a ARG_ERROR `planner_payload` = what the planner saw | M | S | no | no |
| 2 | M1b persist `(loc, code, sent_type)` on a side field, uncapped | M | M | no | no |
| 3 | M2 join each tool call to `provider_served` | M | S | no | no |
| 4 | M3 duplicate-key counter, compose loop (records, never rejects) | M | S | no | no |
| 5 | M4 battery extensions (in `evals/`) | M | M | no | no |
| 6 | M5 turn-cost and Appendix A instruments | M | S | no | no |
| 7 | M6 baseline round (operational) | M | ops | — | no |
| 8 | A1 guidance catalogue, 8 codes (may split into A1a routes, A1b branches, A1c patch) | A | S | yes | no |
| 9 | A2 validation-diagnostic repair calls | A | S | yes (planner and UI) | no |
| 10 | A3 state-validation and "gate is not a plugin" hints | A | S | yes | no |
| 11 | A4 skill lines 478 and 1212 | A | S | yes | no |
| 12 | A5 capability core line 179 | A | S | yes (both planners) | no |
| — | ~~A6 transcript replay encodes every tool~~ **withdrawn** (§8) | — | — | — | — |
| 13 | A7 W-side teaching census (one closed exemption) | A | S | no (a gate) | no |
| 14 | A8 anti-anchor key for wire-stage rejections (fixes a live defect) | A | S | yes (hint choice) | no |
| 15 | P1 reject duplicate keys in the compose loop (only if R-B says reject) | P | S | yes | no, but only useful before F1 |
| 16 | P3 generalised wire-stage rejection branch (envelope its only producer) | P | S | no (identical bytes) | no; only useful before F1 |
| 17 | F1 `patch_node_options` (pilot; carries the one-off machinery) | F | L | yes | yes |
| 18 | F2 `patch_source_options` | F | S | yes | yes |
| 19 | F3 `patch_output_options` (closes the `patch_*` family) | F | S | yes | yes |
| 20 | P4 graph-repair suggestions rendered through `encode` per route (if R-L says encode) | P | M | no (identical output) | no; only useful before F4 |
| 21 | F4 `upsert_node` (first pair maps; first null-writing encode) | F | L | yes | yes |
| 22 | P2 exemplars rendered through `encode` per consumer | P | M | no (identical output) | no; only useful before F5 |
| 23 | F5 `set_pipeline` | F | XL | yes | yes |
| 24-28 | F6 `set_output`, F7 `set_source_from_blob`, F8 `splice_transform`, F9 `set_source`, F10 `set_source_from_blobs` | F | S each | yes | yes (no traffic to measure; acceptance basis per R-G) |

Nothing starts before the start condition in §1.1: John's call that the codebase is stable enough, the S1
zero-property fix landed, and a healthy live window on the 22 remaining strict tools (principle 9). R1 no longer gates
any row. Tier M and Tier A can then proceed in parallel. The ordering constraints that matter:

- M1b needs M1a only as a merge convenience (both touch `turn_audit.py`); M5 needs M1b and M2. M3 is independent of
  M2 (each adds its own key to the persisted tool-call entry); land them in either order and move the key-set pin
  `test_compose_loop_wire_decode.py:348` in each.
- M6 needs M1a to M5 deployed, S1's dev deployment acceptance, and the deployed loop dialect checked (§3.2 M6).
- The Tier A text steps (A1 to A5) are best landed after M6, so that one paired round of the whole tranche can be
  read against the M6 baseline (§5.3). A8 is a defect fix and can land at any time.
- Before any flip: A2, A4 (both lines), A7, A8 and P3 before F1; P1 before F1 if R-B says reject; A1c before F2;
  A1a, A1b, A3 and A5 before F4 and F5; P4 before F4 (if R-L says encode); P2 before F5.

### 3.2 Tier M: measurement (first, once the start condition holds)

M1 was one step in the first draft. The review split it (§8): the planner payload and the violation pairs are
different data with different visibility.

**M1a. The ARG_ERROR `planner_payload` is what the planner saw.**
- *Goal:* make `composition_rejection_events.planner_payload` hold the ARG_ERROR body the planner actually received.
- *Changes:* `turn_audit.build_rejection_records` (`turn_audit.py:53-66`) writes the `arg_error_payload` sent in the
  `tool` message (`tool_error_payloads.py:33-49`), plus `error_category`, instead of `{error_class, error_message}`.
  That makes the table comment at `sessions/models.py:1683-1685` true again.
- *Model-visible:* no. The planner's payload is unchanged; only its persisted copy changes.
- *Why consistent:* JSON content of an existing column, no DDL.
- *Acceptance:* a fixture DB written through `persist_compose_turn`; the persisted payload equals the `tool` message
  content byte for byte (mutate either and the test goes red). Confirm no closed reader of `planner_payload` (as S0 did
  for R3). Full suite plus `testcontainer`.
- *Needs ruling R-I first:* the ticket records that this touches S1 lead ruling 6 ("RejectionRecord untouched").
- *Revert:* one commit. *Size:* S.

**M1b. Persist the violation pairs `(loc, code, sent_type)` on a side field.**
- *Goal:* make the option-tool split in master plan §5.4 measurable, so each flip's acceptance read (§5.3) can be made at all.
- *Why not through the planner payload:* `validation_errors` in the planner's body comes from
  `canonicalize_schema_violations` (`audit.py:1403-1430`), which renders only `loc`, `msg` and `type`, and returns a
  **single** `{"loc": [], "type": "truncated"}` entry when there are more than 8 violations (`audit.py:1413-1421`;
  `_MAX_PYDANTIC_CAUSE_ERRORS = 8`). Over the cap every pair is lost, not only those past the eighth, and a
  `set_pipeline` that stringifies options on several nodes passes 8 easily. Adding `sent_type` there would also make it
  model-visible.
- *Changes:* `SchemaViolation` (`protocol.py`) gains `sent_type`, a closed enum of the 7 JSON types (object, array,
  string, number, integer, boolean, null) plus `absent`, taken only from the S gate's `error.instance`. The dispatch
  outcome carries `ToolArgumentError.schema_violations` on a side field to the ARG_ERROR tool row's result projection
  (`redaction.py:400-410`), which keeps the closed pairs `[{loc, code, sent_type}]` beside `validation_error_count`.
  The side field is uncapped, or capped separately with an explicit total count; it never goes into the `tool`
  message.
- *Sources that carry no `sent_type`:* the pydantic path (`canonicalize_pydantic_cause`, chosen first at
  `tool_error_payloads.py:42`) records its closed `(loc, type)` pairs with no `sent_type`; a `MISSING` violation
  records `absent`. The Appendix A read reports both as their own rows, not as unknown.
- *Model-visible:* no, by construction: the planner-facing renderer is untouched. A pin proves the `tool` message
  bytes are identical before and after M1b.
- *Why consistent:* additive JSON keys, no DDL. The pairs are value-free: loc is closed (S1 C10, a model-authored key
  renders as `field`/`item`), code and `sent_type` are closed enums.
- *Acceptance:* a fixture with 9 violations persists 9 pairs (control: the planner body still shows one `truncated`
  entry). Mutating each persisted field turns its test red. A pin that a model-authored key in loc renders as
  `field`/`item`. Redaction review of the new projection. Full suite plus `testcontainer`.
- *Revert:* one commit. *Size:* M.
- *Tickets:* with M1a and M5, closes `elspeth-a1864430a1` (P2, open).

**M2. Join each tool call to `provider_served`.**
- *Goal:* split every window by enforcing and non-enforcing endpoint. 10 of the 24 deepseek endpoints advertise no
  structured outputs (master plan §2.2).
- *Changes:* put `provider_served` (or the LLM `call_id`) on the D1 wire-facts entry beside `strict_sent`
  (`turn_audit.py:242-243`). Today audit rows are written at turn end and carry no link to the tool calls.
- *Model-visible:* no. *Why consistent:* additive JSON.
- *Pins that move:* `test_compose_loop_wire_decode.py:348`, which pins the persisted entry's exact key set
  `{"id","type","function","strict_sent","wire_conformant"}`.
- *Acceptance:* Appendix A gains a per-`provider_served` split, controlled on a fixture with two endpoints (a mutated
  endpoint value moves the split). *Revert:* one commit. *Size:* S. *Tickets:* none known.

**M3. Duplicate-key counter (compose loop).**
- *Goal:* size ruling R-B. Today the outer decode (`tool_batch.py:918`) passes no `object_pairs_hook`, so a repeated
  key is silently last-wins on every route.
- *Changes:* an `object_pairs_hook` at `tool_batch.py:918` that counts duplicates onto the call's wire facts and
  **never rejects**. The hook returns an exact `dict`, and the exact-type comment at `tool_batch.py:992-996` ("called
  with no object_hook / object_pairs_hook") is reworded to match. The planner's parse (`pipeline_planner.py:1673`) is
  not counted: it is out of S2's scope (§1.3, P1).
- *Model-visible:* no.
- *Why consistent:* nothing extra is admitted; the call keeps today's semantic value. This is instrumentation, not
  dual acceptance.
- *Pins that move:* `test_compose_loop_wire_decode.py:348` (the entry's key set), as for M2.
- *Acceptance:* `{"a":1,"a":2}` records 1 and is still admitted with value 2; control `{"a":1}` records 0.
- *Revert:* one commit. *Size:* S. *Depends on:* nothing (M2 and M3 add independent keys to the same entry).

**M4. Battery extensions** (`evals/composer-battery`, `evals/lib`; not product code).
- *Changes:* fetch rejection reasons; add `composer_strict_tools` and the dialect as named binding fields, so that a
  cross-dialect `--compare` refusal says why (today it is refused silently through `tools_spec_hash`); add seeded
  follow-up scenarios for the tools the corpus never reaches: `set_output`, `patch_output_options`,
  `set_source_from_blob`, `splice_transform`, and one each for `set_source` and `set_source_from_blobs`.
- *Acceptance:* the offline battery unit gate (`tests/unit/evals/composer_battery`); each new scenario reaches its tool
  in a dry run. *Revert:* one commit. *Size:* M.

**M5. Turn-cost and Appendix A instruments** (tracked, controlled scripts; the maintainer picks `scripts/` or
`evals/`).
- *Changes:* burst-first planner-turn cost as defined in `understand-measure.md` §5 (provider round trips from a
  non-ok option-tool call to the first ok call of the same tool in the same user turn; censored if none), plus the
  Appendix A per-`provider_served` and `(loc, code, sent_type)` extensions, plus the **per-turn rate**: the non-ok rate
  of the first attempt of each tool in each user turn. Retries inside a turn are clustered, so the per-turn first
  attempt, not the call, is the unit every gate in §5 uses.
- *Acceptance:* the instrument's controls pass first: `[REJ, REJ, ok]` gives costs `[2, 1]` and burst-first `2`;
  `[REJ, (user)]` gives 1 censored; `[ok, ok]` gives none; `[REJ, REJ, ok]` in one turn gives one per-turn trial,
  non-ok.
- *Needs:* ruling R-J (adopting the turn-cost definition; the ticket and 09-24 plan §1.4 item 9 both ask for it).
- *Revert:* one commit. *Size:* S. *Depends on:* M1b, M2.
- *Tickets:* with M1a and M1b, closes `elspeth-a1864430a1` (its turn-cost ask).

**M6. Baseline round** (operational, no commit).
- *What:* on the deployed S0 + S1 + Tier M build: the battery `canary` at N=10, one full 19×5 round, and the M4
  scenarios.
- *Record per tool:* non-ok rate per call and per turn (M5); the design effect (variance of per-run non-ok counts
  against the binomial); category and code mix; the stringification signature (`invalid_type` at an `options`/`patch`
  loc with `sent_type=string`); burst-first turn cost; drift-hint count (read only after A8, which makes it honest for
  wire-stage rejections); `wire_conformant` by `provider_served`; the duplicate-key count.
- *Needs:* S1's owed dev deployment acceptance.
- *Checked precondition: the deployed loop dialect is `openai_strict`.* The battery measures whatever is deployed; on
  a `none` route every Tier F round measures nothing. `/api/system/status` does not say: its `composer_tool_contract`
  block publishes only the setting and two tool-set counts, deliberately never the route's transport (`app.py:2358-2368`,
  S1 ruling 2). Read the resolved dialect from the `composer_tool_contract_resolved` structured log
  (`service.py:2499`) or from the persisted per-call `tool_contract_dialect` audit fact
  (`llm_response_parsing.py:701-709`) on the baseline round's own rows.
- *The cost input for ruling R-A is a proxy.* For the 10 option tools, `openai_strict` W equals `none` W today
  (`test_wire_projection.py:262-270`), so their `wire_conformant` means only "conforms to S". How many legacy-form calls
  a flip would reject on non-enforcing endpoints is counterfactual until F1 ships. The nearest proxy is
  `wire_conformant = false` per `provider_served` on the 32 tools that are already strict (non-enforcing endpoints
  ignoring a strict W they were sent). F1's legacy-form rejection rate is pre-registered as the first real reading.

### 3.3 Tier A: wire-neutral surface cleanups (alongside Tier M; one surface per step)

Rules for every Tier A step:
- Wire bytes are frozen (principle 4).
- A rewrite names the tool and the **semantic dotted path** (`patch.schema`, `routes.true`, `branches.<name>`). It never
  spells an argument literal (`patch={`, `"options": {`, `routes={`). Where the text is also shown to people, it must
  still read well to them.
- Each text step adds a pin that its strings name a semantic path and contain no `={` argument literal. Control: plant
  `routes={` and the pin goes red.
- Live gate: deterministic per step, then one paired battery round for the tranche A1 to A5 against the M6 baseline
  (§5.3). A step whose tools the battery cannot reach says it is gated deterministically only.

**A1. Guidance catalogue, 8 codes** (`tools/generation.py:964-1264`).
- *Codes:* `unknown_node_type`, `gate_missing_routes`, `gate_route_labels_mismatch`,
  `gate_condition_ignores_stated_threshold` (spell `routes={`: A1a); `coalesce_missing_branches`,
  `fork_branch_no_destination` (spell `branches={`: A1b); `schema_contract_violation`, `sink_contract_violation`
  (spell `patch_source_options(patch={…})`: A1c). It can be one commit or three.
- *Model-visible:* yes, guidance text in tool results on both routes.
- *Why consistent:* the text holds under either spelling; no wire change.
- *Pins that move:* the frozen-bytes fixture `fixtures/generation_response_wire_4f206253.json` holds `routes={'true':
  'fork', 'false': 'fork'}` in cases `explain_exact` and `explain_hint`, and `test_generation_response_contracts.py:108-116`
  asserts its bytes. A1a regenerates it. (The first draft's literal-substring search covered `.py` only and missed this
  `.json` fixture.) Code-set pins unchanged.
- *Revert:* one commit each. *Size:* S. *Must precede:* A1c before F2; A1a and A1b before F4 and F5.

**A2. Validation-diagnostic repair calls** (`web/execution/_validation_diagnostics.py:135, 143, 146, 168, 376, 409`).
- *Today:* literal `patch_node_options(node_id='…', patch={'schema': {...}})` and the `patch_source_options` /
  `patch_output_options` equivalents.
- *Change:* for example "call patch_node_options for node 'x', setting patch.schema to {mode: observed,
  guaranteed_fields: [...]}". The text is also shown in the UI.
- *Model-visible:* yes, to the planner and in the UI. `execution/validation.py:264-272` returns
  `_build_edge_contract_suggestion(…)` as the `suggestion` ("Suggestions expose concrete patch tool calls"),
  `_validation_diagnostics.py:421, 467` put `Tool: {schema_patch_tool_call}` into it, and the model-facing
  forced-repair prompt renders `Suggested fix: {error.suggestion}` (`service.py:1871-1891`,
  `_compose_preflight_repair_message`, appended to `llm_messages`). A2 is therefore in the tranche paired round.
- *Pins that move:* 31 test lines (`test_validation.py` 27, `test_validation_runtime.py` 3,
  `test_validation_edge_contract_disclosure.py` 1).
- *Revert:* one commit. *Size:* S. *Must precede:* F1.

**A3. State-validation route hint and the "gate is not a plugin" hint.**
- `state.py:3574` (`Use routes: {"true": <destination>, "false": <destination>}`) and `tools/_common.py:1987-1991`
  (`routes={'true': ..., 'false': ...}`). The YAML copy of the first text in `core/config.py` stays: YAML is not a tool
  argument.
- *Model-visible:* yes (rejection and state-validation text on any mutating tool). *Pins:* none found.
- *Revert:* one commit. *Size:* S. *Must precede:* F4, F5.

**A4. Skill lines** `skills/pipeline_composer.md:478` (`routes={"true": "fork", "false": "fork"}`) and `:1212` ("the full
replacement options object").
- *Model-visible:* yes (system prompt). `composer_skill_hash` moves; the battery records that as a delta and does not
  refuse the compare.
- *Pins:* `test_prompts.py` has no literal-form pins. Do not edit the skill while any suite is running: that produces
  skill-hash-mismatch errors.
- *Revert:* one commit. *Size:* S. *Must precede:* F1. Line 1212 is about the `patch_*_options` tools, which flip
  first (F1 to F3); both lines go in one commit so the skill hash moves once.

**A5. Capability core line** `skills/pipeline_capabilities.md:179` ("`branches` as `{branch_name: input_connection}`").
- The field tables at `:211-217` **stay in S form**: `validate_capability_field_contract` pins them to S.
- *Model-visible:* yes, in both the composer and the pipeline planner (the core is prepended to both).
  `test_capability_skill_identity.py:403` (hash equals file) follows. The planner's skillpack manifest moves, so re-take
  the tutorial's configuration-relative baseline (ADR-049) as a release smoke.
- *Revert:* one commit. *Size:* S. *Must precede:* F4, F5.

**A6. Transcript replay encodes every tool — WITHDRAWN** (review, §8).
- *What it proposed:* call `encode_semantic_arguments` for every tool at the `semantic=True` transcript rewrite
  (`tool_batch.py:494-501`), not only for `set_pipeline`.
- *Why withdrawn:*
  - As written it crashes the turn on a hallucinated tool name. A name outside `MANIFEST` sets a sentinel
    (`tool_batch.py:866-880`); the call then reaches `audit_arguments = unknown_audit_arguments` (`:1123-1124`), and
    because that object `is not arguments`, the rewrite at `:1145-1151` runs with `semantic=True` and the sentinel.
    `encode_semantic_arguments` raises `WireProjectionError` on a name outside the sent list (`wire_projection.py:861-867`,
    pinned by `test_wire_decode.py:383-388`). A reviewer applied A6 to a scratch copy: 3 of 23 tests in
    `test_compose_loop_wire_decode.py` failed with that error.
  - It has no producer. Every other `semantic=True` rewrite (`:1151`, `:1386`, `:1490`, `:1526`) is a `set_pipeline`
    rewrite, which already calls `encode`. A flipped non-`set_pipeline` tool is never rewritten on its success path
    (`audit_arguments is arguments`), so its transcript keeps the provider's own carrier text.
- *What replaces it:* nothing before F1. F5 adds the null-writing branch to `encode` (from F4) that its `set_pipeline`
  replay needs (§3.5 item 3).

**A7. W-side teaching census.**
- *Today:* the teaching gates read S only (`scripts/cicd/composer_wire_census.py:31`,
  `test_tool_knob_teaching_gate.py:26-40`), so a property that exists only in W would pass them untaught.
- *Change:* a gate over `wire_tool_definitions(d)` for both dialects: every W property is described, and every W-only
  property is taught, with **one closed exemption**, `set_pipeline.pipeline`. That property is S1's envelope: W-only on
  both dialects, with no description (measured at `1f500c6ae`: it is the only undescribed top-level W property on
  either dialect). It is taught by the exemplars. Describing it would change tool-list bytes on both dialects, which
  principle 4 forbids for a Tier A step; F5 may describe it and drop the exemption. The gate proves the exemption set is
  exactly `{set_pipeline.pipeline}`. Each flip then extends the gate with data.
- *Model-visible:* no (a test gate). *Acceptance:* green today with the exemption; plant a W-only property with no
  description → red; remove it → green; add a second exemption → red. *Revert:* one commit. *Size:* S. *Must
  precede:* F1.

**A8. Anti-anchor key for wire-stage rejections** (a live defect, not S2-specific).
- *Today:* the wire-stage rejections write a constant redaction sentinel as audit arguments — the over-bound JSON path
  (`tool_batch.py:935-946`) and the `set_pipeline` envelope rejection (`:1066-1083`) — and then call
  `anti_anchor.record_failure(tool_name, audit.arguments_hash)` (`:981`, `:1107`) with the hash of that constant. Three
  **different** malformed envelopes therefore look identical: the "byte-identical arguments" hint fires
  (`anti_anchor.py:89-90`, `should_inject_hint`) and the drift hint never does (`should_inject_drift_hint` needs more
  than one distinct hash). That is the false control text `elspeth-aa459b4dd0` exists to prevent ("prose for a state
  that did not happen is worse than no prose", `anti_anchor.py:149-156`).
- *Change:* at each sentinel-writing rejection, pass the anti-anchor a key taken from the raw provider text, for example
  `stable_hash(tool_call.function.arguments)`, held in memory only. The persisted `arguments_hash` stays the sentinel's.
- *Model-visible:* yes, the hint choice after repeated wire-stage rejections.
- *Why now:* it is wrong today on `set_pipeline`, and F1 onward multiply these rejections (R-A). M6's drift-hint count
  and the §5.3 drift trip are blind to wire-stage churn without it.
- *Acceptance:* three distinct rejected envelopes fire the drift hint and not the identical hint; three identical ones
  fire the identical hint (control); the persisted audit row still holds the sentinel hash.
- *Revert:* one commit. *Size:* S. *Must precede:* F1 (and should precede M6).

### 3.4 Tier P: flip preparation (in order, ahead of the flip each step serves)

**P1. Reject duplicate object keys in the compose loop** (only if R-B rules "reject").
- *Change:* the rejecting `object_pairs_hook` at the compose loop's outer decode (`tool_batch.py:918`), replacing M3's
  counter. Category `wire_json_invalid`, because the JSON text itself is ambiguous (confirm under R-B). Carrier parses
  later reuse the same hook. The exact-type comment at `tool_batch.py:992-996` is reworded (as in M3).
- *Scope, stated:*
  - **The pipeline planner is out of scope.** Its `_parse_json_object` (`pipeline_planner.py:1669-1678`) runs on every
    planner tool call, the terminal `emit_pipeline_proposal` included (`:1793`, before the terminal split at `:1796`),
    and a failure there raises `PipelinePlannerError(code="MALFORMED_RESPONSE")`, which aborts the whole planner
    response rather than one call. Changing it touches the terminal, which is S3 (§1.3). After P1 the planner stays
    last-wins; the asymmetry is recorded under R-B and belongs to S3.
  - MCP decodes in its SDK and is out of scope.
  - `turn_audit.py:164` re-parses rejected calls for persistence with no hook. That is harmless (it only reads a call
    already rejected or admitted) and stays.
- *Model-visible:* **yes, a behaviour change on the compose loop, on every route:** a call with a repeated key,
  accepted today last-wins, is rejected. M3's baseline count predicts the rate.
- *Why before F1:* so that carrier text inherits one rule, and `{"a":1,"a":2}` behaves the same on every compose-loop
  route and tool.
- *Acceptance:* rejection tests on both dialects, a paired round. *Revert:* one commit (restores last-wins). *Size:* S.
- If R-B instead accepts the asymmetry, P1 is not done and each flip documents that carriers are last-wins like the
  outer decode. R2 condition 3 is about pair labels, not JSON keys, and is met either way; but then a repeated route
  label is rejected on `openai_strict` (a duplicate pair) and last-wins on `none` (a duplicate JSON key). R-B records
  that cross-route difference.

**P2. Exemplars rendered through `encode`, per consumer.**
- *Today:* `planner_authoring_aids.py:2766-2779` hand-wraps `{"pipeline": …}` around the `set_pipeline` exemplars. They
  reach the compose loop through the catalog context (`prompts.py:258`, from `build_messages` ← `service.py:7823`, which
  does not pass a dialect) and the pipeline planner (`pipeline_planner.py:3794`).
- *Change:*
  - the aids store **semantic** exemplars, and the memo stays keyed on semantic content (`:2731`, no dialect in the
    key); each consumer encodes;
  - the compose-loop consumer passes the planner route's dialect;
  - **the pipeline-planner consumer passes the terminal's form, which is S plus the envelope until S3 lands, never
    the route dialect.** Otherwise F5 would teach `options_json` to a terminal that rejects it;
  - `dialect` is keyword-required on `build_messages` / `build_catalog_context_string` (about 44 test call sites),
    following S1 D10;
  - the `purpose` sentence (`planner_authoring_aids.py:2753-2759`: "Each set_pipeline_exemplar* value is a provider
    tool-argument envelope: its pipeline field contains the flat canonical set-pipeline document used directly by
    internal and MCP consumers") becomes form-agnostic, for example "the provider tool arguments for this route". After
    F5 the loop exemplars' `pipeline` field holds carrier strings and pair arrays, and the sentence is memoised
    route-blind (`:2731`);
  - `existing_blob_source_binding` (`:2767`) is a bare `source` fragment, not whole tool arguments, so `encode` cannot
    render it. **Decided here: a semantic mirror**, like the state readbacks. F5 adds the one W sentence that explains
    it ("the same content as the `source` object shown in the authoring aids") and F5's pins cover it. A fragment
    encoder is the larger alternative and is not planned.
- *Model-visible:* no, apart from the reworded `purpose` sentence, which is route-blind text in the catalog context
  (it is in the A-tranche paired round's reach if P2 lands before that round). `encode(set_pipeline)` is the envelope on
  both dialects today.
- *Acceptance:* `test_planner_authoring_aids.py:1916-1945` (NONE-only today) is parametrised over both W; a
  planner-consumer pin that its exemplars validate against the terminal's `pipeline` schema; a loop-consumer pin that
  each encoded exemplar is `wire_conformant` on the loop dialect (it has teeth from F5, when an absent promoted key
  needs the null-writing branch); a SHA pin on the catalog-context bytes per dialect; a mutation that renders the loop
  form into the planner goes red.
- *Revert:* one commit. *Size:* M. *Must precede:* F5. It could ship earlier as a refactor, but has no value until F5.

**P3. Generalised wire-stage rejection branch** (pulled out of F1 by the review).
- *Today:* the envelope rejection (`tool_batch.py:1058-1121`) is a `set_pipeline`-specific branch: sentinel audit
  arguments, a `semantic=False` transcript rewrite, ARG_ERROR with category `wire_envelope`, a fixed message.
- *Change:* make it one branch keyed by a closed (tool, position, cause) → fixed-message table, with the envelope as
  its only entry. F1 then adds rows (`wire_decode` causes) instead of a second branch.
- *Model-visible:* no. Byte-identical today: the envelope is the only producer, and the message, category, audit row
  and transcript rewrite are unchanged. It runs on every malformed envelope, so it is not ahead of its producer.
- *Acceptance:* the existing envelope-rejection tests unchanged; a pin that the table is exactly the envelope row.
- *Revert:* one commit. *Size:* S. *Must precede:* F1. *Depends on:* A8 (the same branch; A8's anti-anchor key moves
  into the general branch).

**P4. Graph-repair suggestions rendered through `encode` per route** (a surface the first draft missed; only if R-L
says "encode").
- *The surface:* `_duplicate_consumer_repair_suggestions` (`tools/_common.py:779-907`) builds ready-to-copy
  `upsert_node` calls: the new gate (`gate_args`, `:826-842`, with `"options": {}` and `"routes": {"true": "fork",
  "false": "fork"}`) and every patched consumer node, serialised whole with its `options` object and a `branches` map
  (`:852-887`). They go into `tool_sequence` (`:893-895`), and `ToolResult.to_dict` puts them into every tool result's
  `validation.graph_repair_suggestions` (`:1094`; the key is listed at `tool_result_envelope.py:56`). The planner reads
  them live, and the comment at `:855-863` says they are copy-ready on purpose. Redaction summarises them to a sentinel
  (`redaction.py:3035-3111`), so persistence is unaffected.
- *Why it matters:* from F4 on `openai_strict`, copying one verbatim is exactly the S-key form R-A rejects (`options`
  at a carrier position, `routes` and `branches` maps at pair positions).
- *Change (R-L option a):* keep the suggestions semantic in `ToolResult`; the point that serialises a tool result into
  the planner's `tool` message, which knows `ctx.tool_contract_dialect`, encodes each `tool_sequence[*].arguments`
  through `encode_semantic_arguments`. `to_dict()` has no dialect today, so this is real plumbing: size M. The
  persisted copy stays S.
- *Model-visible:* no before F4 (`encode(upsert_node)` is the identity on both dialects today). It runs on every
  duplicate-consumer suggestion, so it is not ahead of its producer.
- *Acceptance:* today, byte-identical tool results on both dialects. From F4 (extended in F4's own tests): a scripted
  duplicate-consumer state on `openai_strict` yields a suggestion that passes
  `decode_wire_arguments("upsert_node", OPENAI_STRICT, …)` and is `wire_conformant`; control: render without encode →
  red. F5 extends the pin if a suggestion ever carries `set_pipeline` arguments; F8 if `splice_transform` gains one.
- *R-L option b:* classify the suggestions as a semantic mirror explained once in W, like the readbacks. Cheaper, but
  it contradicts the surface's stated purpose (copy-ready calls), so it is not recommended.
- *Revert:* one commit. *Size:* M. *Must precede:* F4.

### 3.5 Tier F: one tool per flip

#### What every flip contains

A flip adds one row to a closed per-tool carrier/pair table in `wire_projection.py`, applied on `openai_strict` only,
before the free-form check in `project_tool` (`:496`). A tool becomes strict-capable only when none of its free-form
positions is left, so each flip covers every open position of its tool. A carrier on a `strict:false` tool gains no
grammar and is not a step.

1. **W for the tool.** Each option or patch position becomes `*_json: string` (nullable when the S field is optional).
   Each map becomes a closed pair array. Any new projection rule the tool needs lands here with a negative control.
2. **Decode nodes.** `CarrierDecode(path)` and, from F4, `PairsToMap(path, …)`, rejecting as `wire_decode` (R-A). Order:
   envelope, then `StripNull`, then carrier, then pairs. Decode only at declared positions, exactly once; S1's rule
   "never recurses into an undeclared key" carries over.
3. **Encode inverse** in `encode_semantic_arguments`: a carrier becomes compact `json.dumps` of the semantic object;
   pairs are emitted in dict order, never sorted. Two properties, for every flipped tool on its dialect:
   - `decode(encode(s)) == s`. `encode(decode(w)) == w` holds only up to canonical re-serialisation, so master plan
     §3.4's "exact inverse" is reworded.
   - `wire_conformant(encode(s))`: the encoded form validates against the sent W. The first property cannot catch a
     dropped promoted key, because decode strips it anyway. S1 deferred the **null-writing branch** ("write null for an
     absent promoted key") to S2 "with its first reader" (`test_wire_decode.py:14-16`); without it a W-conformant
     `get_plugin_assistance` call with `issue_code: null` decodes and re-encodes to a two-key dict that fails its own
     strict W (measured at `1f500c6ae`). The branch lands in **F4**, whose first reader is the P4 suggestion render, and
     F5 extends it to `set_pipeline`'s index-aware positions and the required-nullable `source`/`sources`, where the
     four `semantic=True` replay sites (`tool_batch.py:1151, 1386, 1490, 1526`) read it. Control: omit the branch → the
     second property goes red.
4. **Carrier states, one test each:**

   | Wire input at a carrier position | Result |
   |---|---|
   | valid object text | the decoded object |
   | `"null"` text | `wire_decode` (not an object) |
   | text that is not JSON | `wire_decode` |
   | JSON that is not an object (a list, a number) | `wire_decode` |
   | wire `null` at a **required** carrier | `wire_decode` (not left to fall through to S as `required`) |
   | wire `null` at an **optional** carrier | `StripNull`, then omitted; no parse |
   | `{"k": null}` inside `patch_json` | survives decode; `_apply_merge_patch` deletes `k` (`tools/_common.py:1638`) |
   | S key alone (`patch: {…}`) on `openai_strict` | `wire_decode` |
   | S key and carrier together | `wire_decode` |
   | `patch_json` on the `none` route | S closed root, `schema_shape` (already true; pin it) |
   | a duplicate key inside carrier text | per R-B |
   | a carrier over 64 KiB | accepted, as the object form is today; the 1 MiB total applies to decoded content (R-E) |
   | a carrier of about 600 KB (boundary test) | accepted; the same content as an object is accepted today (R-E) |
   | outer arguments plus carrier content over the per-call budget | `wire_json_bounds` (the shared-budget control; F5 adds the two-carrier form) |
   | a JSON-looking string at an undeclared position | stays a string |
   | a JSON-looking string **inside** decoded carrier content (`patch_json = '{"prompt_template":"{\"a\":1}"}'`) | stays a string: decode runs once, at the declared position only (the `a5d9e5414` / `e50604428` seam) |

   **Pair states (F4, F5), one test each.** Pairs keep the S key name (`routes`, `branches`), and S admits a map there
   (`routes` is `object|null`, `branches` is `array|object|null`), so a pair decoder that passes a map through to S
   would admit both forms on one route.

   | Wire input at a pair position | Result |
   |---|---|
   | an object (map) | `wire_decode` |
   | wire `null` (the position is nullable in S) | `StripNull`, then omitted |
   | an element missing a field, or with an extra key | `wire_decode` |
   | elements of mixed type | `wire_decode` |
   | a repeated label | `wire_decode` (R2 condition 3) |
   | `[]` at `branches` | `wire_decode` (R2 condition 5) |
   | `[]` at `routes` | decodes to `{}`; S and the handler decide as they do today |

5. **The no-dual-acceptance mutation tests.** A decode that tolerates the S key at a carrier position must turn the
   suite red; so must a decode that passes a map through at a pair position.
6. **Rejection branch.** Add the tool's rows to P3's general wire-stage rejection branch: sentinel audit arguments, a
   transcript rewrite with `semantic=False`, ARG_ERROR with its category, and A8's in-memory anti-anchor key. The
   planner message is a **fixed** string per (tool, position, cause), naming the carrier. It never echoes carrier bytes
   or `JSONDecodeError` text, because `composition_rejection_events.planner_payload` is stored unredacted and, after
   M1a, holds exactly what the planner saw.
7. **Error pointers.** Decode failures carry the wire path in the closed loc vocabulary (`("patch_json",)`,
   `("routes","index","target")`). Content failures stay semantic (`plugin_options_invalid` with `options.<key>`). An
   array index always renders as the token `"index"` (`_dispatch.py:585-599`), so an S error at `("routes","field")`
   maps to `("routes","index","target")` by a fixed rule; no key→index table is needed, and none is built. The rule
   runs where violations are built (`_schema_violation_loc`), because the key is already erased to `field` there
   (`_dispatch.py:610`); size it in F4. In F5, `("nodes","index","routes","index","target")` is five segments and the
   4-segment cap (`protocol.py:1173`) truncates it; F5 states the truncated form it pins.
8. **Audit and `Sensitive`.** Decode already runs before audit canonicalisation (`tool_batch.py:1120` before `:1136`).
   Pin it per tool: an option value inside a carrier never appears as carrier text in a **persisted** row — the
   persisted `arguments_canonical`, redaction output or persisted `tool_calls`. Control: skip decode → red. The
   in-memory `DispatchAudit.arguments_canonical` is a named exception: when the outer JSON is invalid it holds a 4 KiB
   raw prefix (`audit.py:620-633`), which for a flipped tool can be carrier text; the persisted copy is re-redacted
   (`audit_storage.py:202-204, 247-248`). The redaction models type options as `_LlmJsonObject`
   (`redaction.py:1406-1412`), so a string there would fail.
9. **The tool's teaching, strict route only** (§4): `_STRICT_DESCRIPTION_OVERRIDES` entries for the tool's own
   descriptions that name the argument word; a carrier description ("a string containing one JSON object: the same
   content as the `options` object you read back from `get_pipeline_state`"); omission-check handling (option-interior
   prose → an `_OMISSION_VOCABULARY_EXCEPTIONS` entry, promoted-position prose → a "pass null" override); the A7 census
   data. The type-guidance sentence is **not** changed per flip (R-D): an S type fault can never occur at a carrier
   position, so the flip adds a pin that proves it unreachable on `openai_strict`. The import-time check is the
   authority for the hit list: an unhandled hit fails import. S-form text (MCP and `none`) is unchanged.
10. **Pins that move:**
    - `test_wire_projection.py:498` (`strict_tool_count == 32`), `:91` (the `_OPTION_TOOLS` set), `:250` (the
      partition by name) and `:262-270` (`test_option_tools_keep_their_none_parameters_on_openai_strict`);
    - `test_system_status_tool_contract.py:53, 59, 76, 161` (in `tests/unit/web/`);
    - `test_boot_probe.py:218` (`contrast[0].strict_true_count == 32`);
    - `test_llm_tool_contract_audit.py:170` (`expected == 32`) and `:214` (`strict_tool_count == 32`);
    - `test_compose_loop_strict_flip.py:113, 116, 131`;
    - `test_wire_fidelity_matrix.py:381-382`;
    - `test_wire_decode.py:159` (`set(_SEEDS) == _strict_capable_names()`) on every flip, which needs a seed for the
      new tool; `:162` (13 promoted positions) only when the tool adds `StripNull` positions; `:340-342`
      (`test_encode_returns_other_tools_unchanged` iterates `_SEEDS`, so it breaks at F1 when encode stringifies
      `patch`);
    - the tool's scripted-provider compose tests under `openai_strict`.

    The counts of 32 above are the S1 figures measured at `1f500c6ae`. After the S1 zero-property fix (a precondition,
    principle 9) they read 22, and the line numbers move with that fix; re-anchor with `grep -n` before F1.

    `test_error_code_redaction.py:398` (`_OPTION_TOOLS`) is S-scoped and stays.

    **Pins that must not move:** `test_tool_declarations.py`; Check 7 (`test_none_w_is_s_plus_the_envelope`);
    `test_strict_profile.py:349-351`, which partitions **flat S** (`get_tool_definitions()` with
    `check_openai_strict`). If any of them moves, the step changed S and pulled S3 in (§7.3).
11. **Docs and release.** One note in `docs/reference/composer-tools.md` ("on strict web routes this is sent as
    `options_json`"); a release note that operator overlays (`{data_dir}/skills/pipeline_composer.md`) must not spell
    option arguments; a CHANGELOG line (the release is John's call).
12. **Gates.** Principle 5 and 6; `elspeth-lints` corpus before and after (expect judge-bundle churn on
    `decode_wire_arguments`, whose `@trust_boundary` invariant text moves; per-commit hygiene, nothing staged); the
    boot probe accepts the stamped list on each deployed route; a paired battery round (§5).
13. **Live tool-calling canary (principle 9).** Before the flip and again after it, on the deployed route, real tool
    calls against the stamped list for every schema shape the flip changes (carrier string from F1, pair array from F4,
    nested carrier inside a pair element in F5; zero-property if a flip ever touches one), recorded per
    `provider_served` endpoint. The boot probe's acceptance of the list is not this check: S1's 16-token probe accepted
    a list whose zero-property tools then failed every call. Pre-register the canary's pass rule with the step's other
    gates (§5.3). A canary failure on any endpoint is a zero-tolerance tripwire.

**Why a flip is one consistent state.** On `openai_strict` the tool accepts only the carrier form (R-A). On `none` and
MCP it accepts only the object form. The dialect is resolved once per route from settings (`strict_transport.py:190,
281`), and a transcript lives within one turn (`ComposerHistoryMessage` is role and content only, `protocol.py:124-130`),
so no turn mixes forms for a tool. Audit, redaction, the frontend and MCP read S after decode. No DDL, no epoch bump.

**Revert.** `composer_strict_tools=off` at once, then revert the flip's merge. For an earlier flip, when later flips
depend on its machinery, delete its table row, its teaching entries and its pin moves instead. After a flip lands,
revert it before any A or P step it depends on (principle 7); the flip's own module re-asserts those steps' pins.

#### The flips

**F1. `patch_node_options` (pilot).** Size L (irreducible under R-F, §1.1).
- *Positions:* `patch` (required) → `patch_json: string`, not nullable. No new projection rule. 2 option-interior
  omission hits → 2 exceptions.
- *Also carries the one-off machinery,* each as its own reviewable commit under the one merge:
  - the carrier table, the `CarrierDecode` node and the encode carrier branch;
  - the `WIRE_DECODE` member of `ToolArgumentErrorCategory` (today 15 members, `WIRE_*` at
    `contracts/composer_audit.py:115-118`), with its census and registry entries;
  - the `wire_decode` rows in P3's rejection branch, and the wire-path loc vocabulary;
  - `bounded_json_loads(…, budget=, depth_offset=)`: today it builds a fresh `JsonTraversalBudget()` per call
    (`bounded_json.py:152`); the outer decode now creates one budget per call and passes it to every carrier parse of
    that call; every other caller keeps the default. It stays in F1 (§8): nothing passes the parameter before a carrier
    exists, and its counting rule is R-E's sub-decision. Under R-E (a) the outer traversal skips the text of declared
    carrier strings, so a carrier's content is charged once, as decoded content; today `_validate_decoded_json` counts
    every string (`bounded_json.py:92-107`), so without that rule a 600 KB option payload that passes as an object
    fails as a carrier (a reviewer reproduced this with `JsonTraversalBudget`). The raw preflight (`:56-62`, 1 MiB on
    the escaped outer text) still charges each `\"` escape; R-E states that residual;
  - the carrier faithfulness checks (every carrier maps 1:1 onto an S free-form position; the W carrier set equals the
    table);
  - the `@trust_boundary` invariant rewrite for `decode_wire_arguments` (the D12 overturn);
  - the comment at `redaction.py:1406-1408`, which becomes false on the strict route.
- *Not in F1 (removed by the review):* the per-(dialect, tool, position) type-guidance selector. The sentence is
  appended only to S-gate or pydantic type faults (`protocol.py:1080-1091`, from `_schema_tool_argument_error`,
  `tools/_dispatch.py:643-657`), which validate post-decode semantic arguments; after F1 `patch` there is always an
  object, because the carrier decode sends every other state to `wire_decode` with its own fixed message. So the
  `patch_json` arm could never fire. F1 instead pins that an S type fault at the carrier position is unreachable on
  `openai_strict`. Whether the unchanged sentence reads as a contradiction for type faults at other positions of a
  flipped tool is R-D, decided on the F1 round's evidence.
- *Model-visible:* `patch_json` is a string under grammar; `patch: {…}` on the strict route is rejected with a fixed
  message naming `patch_json`.
- *Why the pilot:* no new projection rule; its diagnostics are already neutral (A2); it is the best-instrumented small
  tool (about 45 calls per battery round; the per-call bound in §5.2 is optimistic and is re-derived per turn after
  M6); and it
  carries the most delicate carrier semantics (merge-patch `{"k": null}`), tested once and early. The
  `prompt_template_parts_required` guard (`transforms.py:1655-1670`) is content-side and unchanged; pin it through the
  carrier.
- *Owed before F1:* check whether any length cap applies to the raw `function.arguments` string upstream of
  `bounded_json_loads` (master plan S2, envelope-first O6). No lane measured it.
- *Depends on:* M1a to M6, A2, A4, A7, A8, P3, P1 (if ruled), R-A, R-B, R-E, R-H, the start condition (§1.1), and
  the live canary for the carrier-string shape run before the flip (principle 9).
- *Tickets:* none. A search of open `composer` tickets on 2026-09-25 found no other S2 ticket besides
  `elspeth-a1864430a1`.

**F2. `patch_source_options`.** Size S.
- `patch` → `patch_json`; 1 `StripNull` (`source_name`); 1 option-interior exception. No new rule.
- Barely measurable (about 5 calls per round): deterministic gates plus the qualitative read.
- *Depends on:* F1, A1c.

**F3. `patch_output_options`.** Size S.
- `patch` → `patch_json`; no new rule; 0 omission hits.
- Reached only through an M4 scenario. Closes the `patch_*` family: after F3 the strict route spells every patch the
  same way.
- *Depends on:* F1, M4.

**F4. `upsert_node`.** Size L.
- *Positions:* `options` (optional, so `options_json` is nullable); `routes` → `[{label, target}]`; `branches` →
  `anyOf[list[str], [{branch, input}], null]`.
- *New rules:* nullable for a typeless `anyOf` (`branches`); strip `None` from the S enums `output_mode` and
  `scope_policy` (S1 C20). `upsert_node.on_error` `minLength` (`KEYWORD_NOT_ALLOWLISTED`) is already handled by the
  ledger (`wire_projection.py:364`), so it is a note, not a new rule.
- *First use of the R2 pair codec* (`PairsToMap`), under R2's five conditions (§3.6) and the pair-state table (§3.5
  item 4), and of the fixed pair loc rule (item 7). `upsert_node` has no semantic transcript replay, so
  `test_transcript_argument_order.py` (which covers `set_pipeline` only) is not touched here; it moves in F5.
- *First null-writing encode* (§3.5 item 3): its first reader is P4's suggestion render. The `wire_conformant(encode(s))`
  property covers `upsert_node`, and P4's pin gains teeth here. Under R-L option b there is no P4, so the branch would
  have only a test as its reader; it then lands in F5 instead, where the four `semantic=True` replay sites read it.
- *The tri-state detector gate lands here:* `upsert_node.options_json` is the first promoted **nullable** carrier. The
  gate proves no promoted field has a `null` that means something other than absent, and covers S1's 13 existing
  promotions too. Control: a synthetic promoted field whose null differs from absent → red.
- *Teaching:* about 12 promoted-position or tool-level omission texts need "pass null" overrides (the lane counts 14
  hits by one instrument and 9 by another; import-time Check 5 is the authority). `get_plugin_schema`'s "author raw
  `options`" line (`generation.py:325-327`) gets a strict-route override here, as the first `options` flip.
- *Measurability:* about 15 calls per round: a qualitative gate plus tripwires. The pre-registration says so.
- *Depends on:* F1, A1a, A1b, A3, A5, P4 (or R-L option b), R-L.

**F5. `set_pipeline`.** Size XL. It cannot be split: strict applies to the whole tool (`project_tool:496`).
- *Positions:* carriers at `source.options`, `sources[].options`, `nodes[].options`, `outputs[].options`; pairs for
  `sources`, `nodes[].routes`, `nodes[].branches`; `source`/`sources` required and nullable (the presence XOR stays
  pinned both ways, master plan §3.4).
- *New rules:* the enveloped-tool strict rule (replacing `wire_projection.py:507-508`); root `oneOf` to the ledger;
  `maxLength` with no description; non-root `examples`; index-aware `StripNull` with faithfulness recursion into `items`
  (28 positions); a carrier inside a pair element; 4 carriers sharing one per-call budget (the budget first binds for
  real here).
- *Also:* the exemplar form switches on the compose loop only, through P2; the planner consumer stays on the terminal
  form; the one W sentence for P2's semantic-mirror `existing_blob_source_binding`; optionally a description for the
  envelope, dropping A7's exemption. The 4 `semantic=True` transcript sites (`tool_batch.py:1151, 1386, 1490, 1526`)
  already call `encode`; F5 extends its null-writing branch (from F4) to `set_pipeline`, so a replayed call keeps its
  required-nullable `source`/`sources` and every promoted null. `test_transcript_argument_order.py` is restated for
  array order here.
- *Internal structure:* one merge, several reviewable commits (rules → decode/encode → teaching and exemplars → pins),
  one full-suite gate, one paired round, one revert unit. Its pre-merge proof copies S1's T-series: fidelity-matrix
  rows, a scripted compose loop through the real `run_tool_batch`, custody replay at all 4 `semantic=True` sites.
- *Measurability:* about 90 calls per round, the only step gateable on rate at better than 2× (per call; the per-turn
  bound is re-derived after M6).
- *Depends on:* F4 (pairs, null-writing encode), P2, A1, A5.

**F6 to F10. The remaining `options` tools.** Size S each.
- F6 `set_output` (2 `StripNull`, 1 ledger; no new rule); F7 `set_source_from_blob` (non-root `examples` ledger arm,
  optional carrier, 1 promoted override for `plugin` "Inferred … if omitted"; R7 on MIME inference stays flagged, not
  needed); F8 `splice_transform` (`not` ledger arm for `node.id`); F9 `set_source` (no new rule); F10
  `set_source_from_blobs` (the `examples` arm from F7).
- *Measurability:* none from organic or battery traffic today. `set_source` and `set_source_from_blobs` have never been
  called in any store. Gate: deterministic tests, the boot probe and one M4 scenario each. **A regression here will be
  found only by use.** Each step's plan says so.
- *Why they are a group:* they carry little or no traffic, so no per-tool measurement can show a benefit or a
  regression for them; their acceptance rests on the live canary, the deterministic gates and one M4 scenario each
  (R-G). Do all five in consecutive steps so the strict route ends with one spelling for `options`. If John stops the
  programme after F5, the strict route spells `options` two ways (objects on these five, `options_json` on
  `upsert_node` and `set_pipeline`). That is not dual acceptance, but it is a lasting teaching inconsistency.
- *Alternative order:* F6 to F10 before F4, so that `set_pipeline` reuses the `examples` arm from F7 and the `options`
  family ends its mixed window with `set_pipeline`. The cost is five measurement-blind steps before the measurable ones.

### 3.6 R2's five conditions, as tests (F4 and F5)

1. **Lossless:** `decode(encode(s)) == s` over captured and synthetic maps, for `routes` and both `branches` forms.
   It holds over S minus the empty `branches` collection: on `none`, S admits `branches: []` and the handler stores
   `()`, not `None` (`tools/transforms.py:594-597`), which is observably different (`splice_transform` refuses on
   `branches is not None`, `:1063, :1097`); on `openai_strict`, condition 5 rejects `[]`. That route asymmetry in
   admitted states is a documented behaviour change, in the CHANGELOG with condition 3.
2. **Order both ways:** encode emits pairs in dict insertion order; decode builds the dict in array order.
3. **Duplicate labels rejected** as `wire_decode`. A documented behaviour change, in the CHANGELOG. Separate from JSON
   duplicate keys (R-B).
4. **No default, no choice:** decode never fills an absent pair field and never picks a branch form. Faithfulness: the
   S required set is identical before and after decode.
5. **`branches` by element type:** all strings → a list, kept as a list; all pair objects → a map; mixed → `wire_decode`;
   `[]` → `wire_decode`. Decode never turns a list into a map.

---

## 4. How the teaching split by route is handled

The panel's concern was that teaching would split by route: the strict route would send `options_json` while `none`
routes keep `options` objects, and the skill prompt is route-blind (`prompts.py` has 0 dialect references; control:
`wire_projection.py` has 44). This plan contains the split rather than removing it.

**The rule.** Route-blind surfaces teach *what* goes in a position, by semantic path (`options.profile`,
`patch.schema`, `routes.true`). Only the route's own W teaches *how* the position is spelled. The skill stays one file
with one hash; it never teaches two shapes.

Three layers:

1. **Route-blind surfaces become form-agnostic** (Tier A): the guidance catalogue, validation diagnostics, the state and
   rejection hints, the skill, the capability core. After A1 to A5 none of them states a spelling.
2. **Surfaces that show a concrete argument render it through `encode`** at the point where the route is known: the
   `set_pipeline` transcript replay (already on `encode`; F5 adds the null-writing branch), the exemplars (P2, with the
   planner consumer pinned to the terminal form), the graph-repair `tool_sequence` suggestions (P4, under R-L), and the
   wire-path error pointers.
3. **The encoding instruction lives only in `openai_strict` W for a flipped tool:** `_STRICT_DESCRIPTION_OVERRIDES`
   (`wire_projection.py:217`) and the carrier or pair descriptions. On `none`, a flipped tool's W is byte-identical to
   today's (Check 7).

What remains split, and how each part is handled:

- **Readbacks stay semantic.** `get_pipeline_state`, the state context message and the `applied_component` echo show
  `options` as objects on every route, by design (S1 D1). On a flipped tool the model reads an object and writes a
  string. The carrier description says so once: "the same content as the `options` object you read back". Whether one
  W sentence overcomes that is what the paired rounds measure (§5).
- **The mixed window across tools.** Between F1 and the last flip, the strict list carries carriers on some tools and
  objects on others. Each (tool, route) still accepts one form (R-H). Two signatures are counted per window: `patch:
  {…}` rejected on a flipped tool (from the R-A rejections), and `*_json` sent to an unflipped tool. Today the closed loc
  hides the second as `("field")` (S1 C10); a small optional extension gives it a closed code when the unknown key
  equals a known carrier name. F2 and F3 follow F1 closely to keep the window inside the `patch_*` family short.
- **The permanent MCP split (R-C).** MCP and `none` keep `options` objects for every flipped tool. The precedent is the
  `set_pipeline` envelope, which already differs between web and MCP.
- **The type-guidance sentence.** Master plan S2 said the carrier positions become "exempt by name". That would drop
  the anti-stringification sentence on `none` routes, where options are still objects; that is the seam behind
  `a5d9e5414`. It is also unnecessary: the sentence fires only on S-gate type faults, and none can occur at a carrier
  position on `openai_strict`, because decode rejects every malformed carrier first with its own fixed message. So the
  sentence is unchanged on every route. The one open question is a type fault at another position of a flipped tool
  on `openai_strict`, where "not strings containing JSON" sits beside a W that asks for one; R-D decides it on the F1
  round's evidence.
- **Operator overlays** (`{data_dir}/skills/pipeline_composer.md`, `prompts.py:75, 98`) are outside the tree. Each flip
  ships a release note.

---

## 5. Measurement and the per-step gates

### 5.1 The instrument

- **The tutorial is not an S2 instrument.** It exercises none of the 10 option tools (measured with controls in the
  lane: the tutorial and guided palettes intersect the option set in `[]`, and the tutorial ends by opening a fresh
  composer session without driving the compose loop). Master plan S2's acceptance gate names it; that text should read
  "battery `canary` + per-tool scenarios + the Appendix A census". The tutorial stays a release-boundary smoke for the
  skill and capability-core hash moves (A4, A5).
- **The instrument** is the freeform composer battery (`evals/composer-battery/`), the Appendix A census extended by
  M1b, M2 and M5, and `composition_rejection_events`.

### 5.2 The traffic arithmetic

| Tool | Organic + lane DBs, 2026-09-07..21 | Battery captures (88 runs, August) | Per 19×5 round (est.) |
|---|---|---|---|
| `set_pipeline` | 57 | 84 | about 90 |
| `upsert_node` | 28 | 13 | about 15 |
| `patch_node_options` | 23 | 41 | about 45 |
| `set_source_from_blob` | 13 | 0 | 0 until M4 |
| `set_output` | 8 | 0 | 0 until M4 |
| `patch_output_options` | 5 | 0 | 0 until M4 |
| `patch_source_options` | 2 | 5 | about 5 |
| `splice_transform` | 1 | 0 | 0 until M4 |
| `set_source` | 0 | 0 | 0 until M4 |
| `set_source_from_blobs` | 0 | 0 | 0 until M4 |

The organic column is the R1 panel's figure and includes lane battery DBs; the dev deployment alone made 73 option
calls in 15 days (7 active), about 4.9 a day. The round estimate assumes the captured case mix, which is canary-heavy.

What one paired round can detect (one-sided two-proportion test, α = .05, power .8, from `step_power.log`; its controls
reproduce the panel's 169 and 678 as 170 and 685). **These are per-call figures and therefore optimistic.** The test
assumes independent calls, but failures cluster: a rejection is usually followed by same-tool retries in the same turn
(M5's burst model). The gating unit is the first attempt of a tool per user turn (M5), with fewer trials than calls.
The table below is kept as an upper bound on sensitivity; every threshold is re-derived on the per-turn unit, or with
the design effect M6 measures, before it is pre-registered.

| Calls per arm | Smallest non-ok rate distinguishable from a 0.20 baseline |
|---|---|
| 10 | about 0.73 |
| 20 | about 0.58 |
| 45 (one round of `patch_node_options`) | about 0.45 |
| 90 (one round of `set_pipeline`) | about 0.37 |

- From a 0.20 baseline, 0.30 needs 231 calls per arm and 0.40 needs 64.
- So one round can gate F1 and F5 on a gross regression (about double the failure rate). F4 (about 15) and F2 (about
  5) get a qualitative gate. F3 and F6 to F10 get deterministic gates and seeded scenarios only.
- **The limit, stated once:** a round can show that a step did not break its tool. It cannot show that a step helped.
  The benefit direction needs 170 or more calls per arm for `set_pipeline` alone.
- The former R1 reopen trigger (about 50 failures per tool; master plan §6.3, no longer a precondition since
  2026-09-25) is further still, and is now the size of a per-tool benefit read. At the battery's roughly 20%
  option-tool failure rate that is about 250 calls of one tool. At 4.9 option calls a day in total, 250 calls of any
  one tool takes at least 51 days even if every option call were that tool; in practice it is months, and never for a
  tool nobody calls. That is the organic horizon. If battery traffic counts as acceptance evidence,
  `patch_node_options` (about 9 failures per round) reaches about 50 in about 6 rounds. R-M asks which.

### 5.3 Gates per step

- **Pre-register** each step's thresholds, re-derived from the M6 baseline, before its first round.
- **Paired rounds on the same build lineage:** the control arm is the build before the step, same route and model,
  close in time. Wire steps change `tools_spec_hash`, so `--force-compare` is expected and recorded; after M4 the
  refusal names the dialect field.
- **Split every read** by route (`strict_sent` 1, 0 or NULL) and by `provider_served` (after M2).
- **Tier M:** fixture controls only; M6 is the baseline.
- **Tier A text steps:** deterministic gates per step, then one paired round for the tranche A1 to A5 (A2 included:
  it is planner-visible) against M6. A8 is gated deterministically; its effect shows as drift hints in the rounds.
  Each step is its own commit, so a trip is bisectable. That gives the same statistical power as a round per step for
  about a sixth of the operator hours. The tranche round then serves as the control arm for F1. Trip: the
  tool's non-ok rate above the §5.2 bound for its n, a rise in burst-first turn cost, or drift hints going from
  near-zero to recurring.
- **Tier F, zero-tolerance tripwires (revert on one occurrence, any n):**
  - a `wire_decode` caused by a server encode/decode asymmetry (the round trip fails on a captured argument);
  - a `_BadRequestLLMError` or boot-probe rejection naming the new schema;
  - a live canary failure for a changed schema shape on any `provider_served` endpoint (principle 9; §3.5 item 13);
  - carrier text in any persisted audit row.
- **Tier F, regression bound** for rate-gateable tools (F1, F5): the tool's non-ok rate on strict-route rows within the
  bound for the arm size, against the paired control.
- **Tier F, legacy-form rejection rate (the cost of R-A):** `wire_decode` rejections where the S key was sent, split by
  `provider_served`. High on non-enforcing endpoints together with a turn-cost rise → revert.
- **Tier F, descriptive reads:** the stringification signature on the tool should go to zero on enforcing endpoints;
  `wire_decode` counts beside the previous window's `JSONDecodeError` + envelope + `schema_shape` counts on that tool
  (errors may move rather than vanish); burst-first turn cost; drift hints; the mixed-window signatures (§4).
- **Windows are tagged by flip commit,** because `strict_sent` and `wire_conformant` change meaning per tool per window.
- **Deferred organic read:** the same measures on dev-session data over the following weeks, reported, not gating.
- **Per-flip benefit read (the former R1 reopen trigger, now a measurement).** Per flipped tool, the master plan §5.4
  split (shape against content), turn cost and the enforcing-endpoint share, read against the pre-flip window and the
  2026-09-24 evidence (10 shape, 18 content, 2 ambiguous; the stringification signature of `a5d9e5414` /
  `e50604428`). A flip that shows a regression is reverted at once (the tripwires and bound above). A flip whose read,
  once it has enough failures to say anything (§5.2), shows no benefit is reverted too (principle 7). This is an
  acceptance read, not a gate on starting the flip.

---

## 6. S3 after S2

- **S3 stays independent while every step is W-only.** The planner terminal is built from
  `canonical_set_pipeline_schema()` (S) and stamped `strict:false`; `capability_skill.py:248` hashes the advertised
  terminal against canonical S and `:261` binds it; `validate_capability_field_contract` pins
  `pipeline_capabilities.md:211-217` to S; the planner's discovery palette shares no tool with the 10 option tools
  (intersection measured empty, with a control).
- **The one coupling is the exemplars.** After F5, the pipeline planner must keep receiving S-form exemplars (P2),
  otherwise it is taught carrier text its terminal rejects.
- **S3 is reinstated with S2 (R1 yes, deferred) and starts only after F5.** It reuses F5's `set_pipeline` rules. It
  rebinds the manifest check to `project_tool(canonical)` for the sent dialect (master plan §4 S3), switches the planner
  consumer's exemplar form in the same commit, and sends a strict terminal only to a hatch route that the boot probe
  and a live canary (principle 9) have exercised with it.
- **Tripwire:** any step that moves `test_tool_declarations.py` or Check 7 has changed S and pulled S3 in. Stop and
  re-plan.

---

## 7. Risks, open decisions and stop conditions

### 7.1 Risks

1. **Nothing measurable changes.** Most option-tool failures are content errors, which no grammar reaches. The
   programme may deliver the contract without a visible rate change. The per-step rounds can show only that nothing
   broke.
2. **R-A rejects calls accepted today** on the 10 of 24 deepseek endpoints without structured outputs. M6 sizes this
   before F1; the legacy-form rejection rate is watched per flip.
3. **The mixed window confuses the planner** (a flipped tool beside unflipped siblings, objects read back and strings
   written). Counted per window (§4); if material, finish the family or pause.
4. **Teaching the read-object/write-string asymmetry** in one W sentence may not be enough. Only the paired rounds can
   tell.
5. **Blind flips** (F3, F6 to F10) regress without any signal until use.
6. **F5's size.** It is roughly the size of S1's option-free half and cannot be made smaller without a test-only "dry
   projection" of `set_pipeline`, which is production code whose only consumer is a test. Not recommended.
7. **Judge-bundle churn** on `decode_wire_arguments` at F1 and every later flip that edits it: ordinary per-commit
   hygiene, nothing staged.

### 7.2 Open decisions for John

| # | Decision | Gates | Recommendation |
|---|---|---|---|
| R1 | **Ruled yes, deferred (John, 2026-09-25; master plan §6.3).** No per-tool or per-family reopen is needed. What remains is the start condition (§1.1): John's call that the codebase is stable enough, plus the S1 zero-property fix and a healthy live window (principle 9) | when Tier M and A start; every flip needs its live canary | Nothing to decide beyond the start call |
| R-A | On a flipped tool on `openai_strict`, the S-form key (alone or with the carrier) is rejected as `wire_decode`, as is a non-string at a required carrier and carrier text that is not a JSON object. **Overturns S1 D12**; the `@trust_boundary` invariant text changes | F1 and every flip | Yes. It is the only form of a flip that meets "no dual acceptance". It rejects calls accepted today on non-enforcing endpoints; M6 sizes that, and the rate is a pre-registered tripwire |
| R-B | Duplicate JSON object keys: reject in the compose loop on every route (P1), or accept and document last-wins in carriers. Either way two asymmetries are recorded: the pipeline planner's parse stays last-wins (changing it touches the terminal and its `MALFORMED_RESPONSE` whole-response abort, which is S3); and if "accept", a repeated route label is rejected on `openai_strict` (a duplicate pair) but last-wins on `none` (a duplicate JSON key) | F1 | Decide on M3's count. Preferred: P1, compose loop only (`tool_batch.py:918`) |
| R-C | Accept the permanent split: web `openai_strict` sends `*_json`, MCP and `none` send objects | F1 | Accept. The alternative is a carrier in S, which breaks "`none` = today's bytes" and pulls S3 into every step |
| R-D | R1's "The 'not strings containing JSON' guidance stands" needs no amendment at carrier positions: an S type fault can never occur there on `openai_strict`. Open: for a type fault at another position of a flipped tool on `openai_strict`, keep the sentence, or append a fixed per-(dialect, flipped tool) variant that names the carrier exception | after the F1 round | Keep the sentence unchanged in F1 and pin the carrier position unreachable. Build the variant only if the F1 round shows the sentence confusing the model; it needs the allowlist (`protocol.py:993-997`) and strip (`:1392-1393`) changes plus a redaction review |
| R-E | A bounds overrun inside a carrier stays `wire_json_bounds`. Sub-decision on the shared per-call budget: (a) the outer traversal skips declared carrier strings and charges their decoded content once, or (b) accept a roughly halved effective limit for carrier tools | F1 | Yes, and (a): it is what "accepted, as the object form is today" in the carrier table already promises. The raw 1 MiB preflight still charges escapes; state that residual |
| R-F | No machinery lands ahead of its first producer | step sizes | Confirm (S0 precedent). This is why F1 (L), F4 (L) and F5 (XL) cannot be made smaller, and why the pair codec waits for F4. Everything with a live producer today has been pulled out (P3, P4, and the defect fix A8). Relaxing R-F is the only way to shrink the three further; that is the direct trade against "small steps" |
| R-G | The per-flip acceptance rule, replacing "the basis for each reopen" now that R1 is yes: a flip is kept when its live canary passes on every `provider_served` endpoint, no zero-tolerance tripwire fires, and its non-ok rate stays within the §5.2 bound against the paired control; it is reverted on a regression, or when its benefit read (§5.3) shows no benefit once it has the data. For F6 to F10, which have no traffic to measure, acceptance is the live canary, the deterministic gates and one M4 scenario each | every flip; F6 to F10 | Adopt as stated. For F6 to F10, John may still choose to stop after F5 (§3.5) |
| R-H | "No dual acceptance, no old wire form alongside the new one" (master plan §4) is read per (tool, route); the mixed window across tools is what "one at a time" entails | every flip | State it explicitly, so it is not relitigated |
| R-I | M1a changes the ARG_ERROR `planner_payload` in `RejectionRecord`, which S1 lead ruling 6 left untouched (the ticket records this as needing a lead decision) | M1a (and M1b, which shares the rejection path) | Allow it: the payload becomes what the planner saw, which is what the table comment already promises |
| R-J | Adopt the planner-turn-cost definition (`understand-measure.md` §5; 09-24 plan §1.4 item 9) | M5 | Adopt burst-first round trips, censored failures reported apart |
| R-K | Full-suite gate on every one-string text step (A3 to A5), or grouped: focused tests plus the affected whole-tree gates per commit, one full-suite gate for the group. AGENTS.md says "choose tests by the reach of the change"; the master plan requires the full gate per slice | Tier A cost | The plan follows the stricter rule until John rules. The gate time exceeds the diff time for these steps |
| R-L | Graph-repair `tool_sequence` suggestions (a surface the first draft missed): (a) render them through `encode` per route at the point that knows the dialect (P4), or (b) classify them as a semantic mirror explained once in W | F4 | (a). The surface exists to be copied verbatim (`tools/_common.py:855-863`), so a mirror the model must transcode by hand defeats it; (a) follows §4 layer 2. It costs dialect plumbing that `ToolResult.to_dict` lacks today (P4, size M) |
| R-M | Does battery traffic count as acceptance evidence for a flip (the §5.3 benefit read and the R-G rule), or only organic traffic? (Before 2026-09-25 this asked whether it counts toward the R1 reopen trigger.) | how soon a flip's benefit read can be made | John's call. Organic only: months per tool, never for uncalled tools. Battery counted: about 6 rounds for `patch_node_options` |

### 7.3 Stop conditions (stop and report; do not improvise)

- `test_tool_declarations.py`, Check 7 or `test_strict_profile.py:349-351` moves: the step changed S.
- A step would need either argument form to be accepted for the same tool on the same route, even briefly.
- Any Tier M, A or P step changes tool-list bytes on any dialect.
- Carrier text appears in any persisted audit row, redaction output or persisted `arguments_canonical` (the
  in-memory 4 KiB raw prefix for invalid outer JSON is the named exception, §3.5 item 8).
- A `_BadRequestLLMError` or boot-probe rejection names a new schema.
- A live canary (principle 9) fails for a changed schema shape on any `provider_served` endpoint.
- A round-trip failure on a captured argument.
- A fix would need the server to insert, choose or repair a value (composer invariant 1), or a tutorial-only path
  (invariant 2).
- A flip's import-time checks (Check 5, faithfulness, limits) fail and the only way through is a suppression.

### 7.4 Corrections to the lane notes and the master plan

1. `test_strict_profile.py:349-351` partitions flat S (`get_tool_definitions()` + `check_openai_strict`). It must not
   move on a W-only flip. `understand-surfaces.md` and `design-per-surface.md` list it as moving.
2. `test_wire_decode.py:159` (`set(_SEEDS) == _strict_capable_names()`) moves on every flip, not only when a tool adds
   `StripNull` positions; `:162` (13 promoted) moves only then.
3. Master plan S2's "exempt by name" for the type-guidance sentence would drop it on `none` routes (§4).
4. The sentence is built when `ToolArgumentError` is constructed, with no dialect (`protocol.py:1091`), so choosing it
   per dialect is not a parameter in `tool_batch`. It is not needed at carrier positions at all (R-D); if the
   per-tool variant is ever ruled in, it carries a redaction review.
5. Master plan S2's acceptance gate names the tutorial, which is inert for S2 (§5.1).
6. Master plan §3.4's "exact inverse" holds only as `decode(encode(s)) == s`.
7. The plan's `planner_authoring_aids inline_form.example_options` row points at `tools/_common.py:1909-1911`; it is
   option content, valid inside a carrier, and needs no step.
8. `interpretation_state.py:614` and `execution/service.py:3386` (now `:3393`) only name tools; they are not surfaces.
9. The knob-teaching gate and the wire census read S; they are extended (A7), not moved.
10. R2 condition 3 cites `tool_batch.py:914` for the hook-less decode; it is `:918` in this tree.
    `bounded_json_loads` now takes `object_pairs_hook` but still has no budget parameter.
11. Ticket `elspeth-a1864430a1` is readable (the lane notes could not reach it): it asks for the `(loc, code)` pairs
    plus the sent JSON type, notes the S1 ruling-6 conflict (R-I), and asks for the turn-cost definition (R-J).
12. **Owed to the master plan (tracked, not edited here):** §5.4 says the violation cap loses only the pairs past the
    eighth. In the code, more than 8 violations collapse to one `truncated` entry and every pair is lost
    (`audit.py:1413-1421`). M1b is designed around the correct reading.
13. The null-writing encode branch S1 deferred to S2 (`test_wire_decode.py:14-16`) was not scheduled in the first
    draft of this plan; it now lands in F4 and is extended in F5 (§3.5 item 3).

### 7.5 Not settled here

- Whether any length cap applies to the raw `function.arguments` string upstream of `bounded_json_loads`. Owed before
  F1.
- The deployed route (`deploy/elspeth-web.env`) was not read by any lane. The "OpenRouter deepseek, `FORWARDING`"
  premise comes from master plan §2.2. M6 now checks the resolved loop dialect as a precondition (§3.2), from the
  structured log or the per-call audit fact, not from `/api/system/status`.

---

## 8. Plan review

Two independent critiques reviewed the first draft against the code at `1f500c6ae`: one for consistency and the
composer invariants, one for reality and size. Together they raised 40 findings (2 blockers, 20 major, 18 minor), 31
distinct after merging overlaps. Each was re-verified at the cited source before it was applied. 36 were applied as
proposed, 2 with a corrected remedy, 2 in part, and none was rejected outright. The per-finding dispositions are in the
lane (`$L/revise-dispositions.md`), which is lost with the worktree; this section carries the outcome.

**What changed in the step list:**
- **A6 withdrawn.** As written it crashed the turn on a hallucinated tool name (the unknown-tool sentinel reaches the
  `semantic=True` rewrite at `tool_batch.py:1145-1151`, and `encode` raises on an unsent name), and it had no producer:
  every other `semantic=True` rewrite is `set_pipeline`, already encoded.
- **M1 split** into M1a (the persisted `planner_payload` is what the planner saw) and M1b (the violation pairs on a
  side field). Over 8 violations the planner's body keeps a single `truncated` entry, so the pairs cannot ride on it,
  and adding `sent_type` there would have made M1 model-visible.
- **Added:** A8, a live-defect fix (distinct malformed wire-stage calls hash to one constant sentinel, so the
  anti-anchor asserts "byte-identical arguments" and the drift hint never fires); P3, the generalised rejection branch
  pulled out of F1 with the envelope as its live producer; P4, the graph-repair `tool_sequence` suggestions, a
  copy-ready `upsert_node` surface in every tool result that the first draft missed and that F4 would otherwise
  contradict (R-L).
- **Moved:** A4 before F1 (skill line 1212 is about the `patch_*` tools). The null-writing encode branch S1 deferred to
  S2 is now scheduled (F4, extended in F5), with a second encode property, `wire_conformant(encode(s))`.
- **Removed from F1:** the type-guidance selector. An S type fault can never occur at a carrier position, so it had
  no reachable output; F1 pins that instead, and R-D keeps one open question for the F1 round.
- **Tightened:** P1 scoped to the compose loop (the planner's parse includes its terminal, which is S3); a pair-state
  table closes the one dual-acceptance hole (a map under the pair key name); A7 carries a closed exemption for the
  `set_pipeline` envelope; rollback order after a flip; the persisted-row scope of the carrier-text pin; the shared
  per-call budget's carrier counting rule (R-E); the full list of partition pins; the per-turn gating unit; the
  deployed-dialect precondition for M6.
- **Resized:** F1 from M-L to L. F1, F4 and F5 are stated as the irreducible large steps under R-F (§1.1).
- **Not taken:** building the per-tool type-guidance variant inside F1 (no evidence yet that it is needed; R-D), and
  pulling the `bounded_json_loads` budget parameter out as its own step (nothing would call it before F1).
- **New rulings:** R-L (graph-repair rendering) and R-M (does battery traffic count toward the reopen trigger).

**Reframe 2026-09-25.** The plan was first written as contingent on R1, which John had ruled no on 2026-09-24. On
2026-09-25 he reversed that ruling to yes, deferred (§1.1 quotes him; master plan §6.3). This revision:
- rewrites §1.1: S2 and S3 are reinstated, in small steps; the start condition is codebase stability (John's call),
  not the data-driven reopen trigger;
- turns the 2026-09-24 evidence (10 shape, 18 content, 2 ambiguous; `strict` on a carrier string constrains nothing
  inside it; the options-as-string swings `a5d9e5414` / `e50604428`) into per-step acceptance and measurement input
  (§5.3 benefit read) instead of a gate, and the former per-tool reopen trigger into a measurement read at each flip;
- adds principle 9 and Tier F item 13, the live tool-calling canary per schema shape and per `provider_served`
  endpoint, from S1's zero-property regression (session `ed3c015b-a2f7-4322-9f39-1716541c5796`), and makes that
  regression's fix plus a healthy live window on the 22 remaining strict tools a precondition for starting;
- rewords R1, R-G and R-M in §7.2 (R1 ruled; R-G becomes the per-flip acceptance rule; R-M asks about acceptance
  evidence), renames the §3.1 gating column, and reinstates S3 after F5 (§1.4, §6).

---

## Appendix: surface inventory

One row per surface from `understand-surfaces.md` §3, with the step that cleans it. "No step" means the surface reads
S, or states only semantic content, and S2 does not touch it.

### Wire machinery

| Surface | Today | Step |
|---|---|---|
| `project_tool` free-form check, `wire_projection.py:494-508` | 10 option tools flat, `strict:false` | each flip (F1 to F10) adds a table row |
| Decode plan nodes; `decode_wire_arguments`; `encode_semantic_arguments` | envelope and null-strip only | `CarrierDecode` in F1; `PairsToMap` in F4 |
| `_STRICT_DESCRIPTION_OVERRIDES`, omission vocabulary and exceptions, faithfulness gate | 6 overrides, 1 exception, non-option tools only | each flip, for its own tool |
| Limits report (`wire_projection.py:726-756`) | 32-tool strict budget | re-measured at each flip (fails at import if exceeded) |
| Transcript replay `tool_batch.py:494-501` | encodes `set_pipeline` only; other tools are never rewritten on success | no change for other tools (A6 withdrawn); null-writing branch in F4, extended to `set_pipeline` in F5 |
| Wire-stage rejection branch (`tool_batch.py:1058-1121`) and its anti-anchor key (`:981, :1107`) | `set_pipeline`-specific; constant sentinel hash | A8 (anti-anchor key); P3 (generalised branch); F1 adds `wire_decode` rows |
| Outer decode `tool_batch.py:918`; `bounded_json.py:116-152` | last-wins duplicates; one budget per function call | M3 counter; P1 if ruled (compose loop only); budget parameter and carrier counting rule in F1 (R-E) |
| Planner argument parse `pipeline_planner.py:1669-1678` (every planner call, terminal included) | last-wins; failure is `MALFORMED_RESPONSE` | no step (S3) |
| `ToolArgumentErrorCategory` (`contracts/composer_audit.py:79`) | 15 members, no `wire_decode` | F1 |
| `_schema_violations` (`_dispatch.py:617-662`) | loc is the S path | wire-path pointers in F1; fixed pair loc rule in F4 (no key→index table) |
| Boot probe (`service.py:876`) | sends the stamped list | no code change; its outcome per route is each flip's acceptance |
| `llm_response_parsing.py:700-712` (dialect, `strict_tool_count`) | audit fact | no step (counts move, nothing pinned) |
| Tri-state detector | not built | F4 |

### Planner-facing teaching, route-aware (W)

| Surface | Today | Step |
|---|---|---|
| Option tools' own tool and property descriptions (26 argument-word sites on 7 tools) | object and map vocabulary | each flip, through strict-route overrides; S text unchanged |
| Non-option tools naming the argument (`get_plugin_schema` "author raw `options`", `generation.py:325-327`; others semantic) | mostly semantic | `get_plugin_schema` override in F4; the rest no step |

### Planner-facing teaching, route-blind

| Surface | Today | Step |
|---|---|---|
| Authoring-aid exemplars (`planner_authoring_aids.py:2766-2779`; `prompts.py:258`; `pipeline_planner.py:3794`) | hand-wrapped provider envelope | P2; the loop form switches in F5 |
| `_TOOL_ARGUMENT_JSON_TYPE_GUIDANCE` (`protocol.py:904-906, 993-997, 1091, 1392-1393`) | "not strings containing JSON", on S-gate and pydantic type faults | no change; F1 pins the carrier position unreachable; per-tool variant only if R-D rules it in after F1 |
| Authoring-aids `purpose` sentence (`planner_authoring_aids.py:2753-2759`) | "a provider tool-argument envelope … flat canonical set-pipeline document" | P2 (form-agnostic wording) |
| Graph-repair suggestions (`tools/_common.py:779-907`, in every tool result's `validation.graph_repair_suggestions`, `:1094`) | copy-ready `upsert_node` calls with `options` objects and `routes`/`branches` maps | P4 (R-L), before F4 |
| Anti-anchor hint choice after wire-stage rejections (`anti_anchor.py:64-90`) | false "byte-identical" hint for distinct malformed calls | A8 |
| Skill `pipeline_composer.md:478, 1212` | spells `routes={…}`; "replacement options object" | A4 (before F1) |
| Rest of the skill (41 option lines, semantic paths; `:864-883` is option content) | semantic | no step |
| Capability core `pipeline_capabilities.md:179` | `{branch_name: input_connection}` | A5 |
| Capability core field tables `:211-217` | pinned to S | no step (must stay S) |
| Guidance catalogue, 8 codes (`generation.py:964-1264`), and its frozen-bytes fixture `generation_response_wire_4f206253.json` | spells `routes={`, `branches={`, `patch={…}` | A1 (A1a, A1b, A1c; A1a regenerates the fixture) |
| Guidance catalogue, 44 other codes mentioning the words as concepts | semantic | no step |
| Validation diagnostics (`_validation_diagnostics.py:135-168, 376-409`), reaching the planner through the forced-repair prompt (`service.py:1871-1891`) | literal `patch_*_options(…, patch={…})` | A2 |
| State-validation message `state.py:3574` | `Use routes: {…}` | A3 (the YAML copy in `core/config.py` stays) |
| "gate is not a plugin" hint `_common.py:1987-1991` | `routes={…}` | A3 |
| Credential repair `_common.py:1860-1871` and `inline_form.example_options` `:1909-1911` | semantic; option content | no step |
| Other rejection texts naming tools (`blobs.py:870-877`, `sources.py:1983-1988`, `no_tool_policy.py:1467, 1520`) | names and semantic paths | no step |
| ARG_ERROR expectations with semantic paths (`options.model`, …) | semantic | no step |
| Comment `redaction.py:1406-1408` | "stringified … now reject at admission" | F1 |
| Operator skill overlay `{data_dir}/skills/pipeline_composer.md` | outside the tree | a release note per flip |

### Response mirrors, audit, frontend, S surfaces and gates

| Surface | Step |
|---|---|
| State context message, `get_pipeline_state`, `applied_component` echo | no step (semantic by design; explained once in each flip's W) |
| `arguments_canonical`, redaction, proposal `arguments_redacted_json` | no step (post-decode semantic; pinned per flip) |
| Persisted assistant `tool_calls` and wire facts | M2 adds `provider_served`; M3 the duplicate count; each moves the key-set pin `test_compose_loop_wire_decode.py:348` |
| Frontend (`ProposalDiff.tsx`, `ToolCallCard.tsx`, `redactedArguments.ts`, `toolCallDescriptions.ts`) | no step |
| `composition_rejection_events` ARG_ERROR `planner_payload` | M1a |
| Redacted ARG_ERROR tool row result projection (`redaction.py:400-410`) | M1b (side field; the planner body is unchanged) |
| Flat registry, pydantic models, redaction MANIFEST | no step (S) |
| MCP sidecar (`composer_mcp/server.py`) | no step (keeps S permanently; R-C) |
| `docs/reference/composer-tools.md` | a one-line note per flip |
| `test_tool_knob_teaching_gate.py`, `composer_teaching.py`, `composer_wire_census.py` | no move; extended by A7 (closed exemption `{set_pipeline.pipeline}`) |
| `composer_admitted_wire.py`, `composer_frontend_wire.py` | no step |
| Partition pins (§3.5 item 10, including `test_boot_probe.py:218`, `test_llm_tool_contract_audit.py:170, 214`, `test_wire_projection.py:91, 250, 262-270`, `test_wire_decode.py:340-342`) | each flip |
| `test_transcript_argument_order.py` (covers `set_pipeline` only) | F5 (array order) |
| `test_planner_authoring_aids.py:1916-1945` | P2 |
| Scripted-provider compose tests under `openai_strict` | each flip, where they script that tool |
| `generate_skill_inventory.py --check` | not triggered (no tool is renamed) |
| Planner terminal, `capability_skill.py:248, 261` | no step (S3) |
