# RV1 — planner surface (F1, F7, surface-teaching-crosscheck, R1 R4 R8 E3 P1 P5)

Reviewer: RV1-planner-surface. Tree: `ccr-d14093ba-glu6bw` @ `45b65e50` (code = `main` @ `1a7ff0e6`).
Read-only. Helper scripts (scratch, not project files):
`<session scratchpad>`,
`.../rv1/palette_detail.py`.

Abbreviations: `LIVE` = `.claude/lanes/composer-desk-audit-20261112/live`; `S10` = `LIVE/sessions/s10-two-files`;
`SP` = `render_system_prompt(None)` rendered now (135,863 chars, sha256 `2e4c56762a74…`).

## Verdict table

| # | Claim | Verdict | One-line reason |
|---|---|---|---|
| F1.1 | Planner offered 23 tools, not `set_source_from_blobs` | CONFIRMED | EXECUTED `planner_tool_definitions(dialect=NONE)` → 23 names, no `set_source_*`/`create_blob`/`wire_blob_inline_ref` |
| F1.2 | Named `sources` entries forbid `blob_id` (sessions.py:2188) | CONFIRMED | EXECUTED: terminal schema `oneOf [{required:[source]},{required:[sources]}]`; `sources` items = plugin/options/on_success/on_validation_failure/description only |
| F1.3 | blob_rows hint points at `set_source_from_blobs` (blob_rows.py:176) | CONFIRMED | Also in the digest `not_for` the planner gets in `authoring_aids` (S10 turn01 msg[1]) |
| F1.4 | Multi-file is unbuildable on the planner | CONFIRMED (every path checked) | See §F1.4; only one blob can ever be bound (singular `source`) |
| F1.5 | T1: "9 calls over 241.6 s", 504 deadline | CONFIRMED-WITH-CORRECTION | 9 *captured*; a 10th call (2nd hatch call) ran 20:45:19.5→20:46:31 and was cancelled by the deadline |
| F1.6 | "Its reasoning says so directly: …That tool isn't available in this palette" | CONFIRMED-WITH-CORRECTION | Quote is from the **hatch** (z-ai/glm-5.3, T1 seq 13), not the planner's primary model |
| F1.7 | T2: 167.7 s, HTTP 500 `planner_repair_exhausted` | CONFIRMED | `S10/turn02/terminal.json` |
| F1.8 | Hatch engaged "after three rejected proposals (repair budget 2)" | CONFIRMED-WITH-CORRECTION | True for T1; T2 = 1 truncated response + 2 rejections |
| F1.9 | Hatch ran on the advisor model with the full 23-tool palette | CONFIRMED | pipeline_planner.py:4271-4277 + planning_application.py:1152; all 4 captured hatch calls: glm-5.3, 23 tools, same system hash |
| F1.10 | T1 hatch "took 69 s and spent the remaining wall clock" | CONFIRMED-WITH-CORRECTION | First hatch call 69.2 s (3 reads); second hatch call ~72 s, cancelled |
| F1.11 | T2 hatch "took 71 s and ended in REPAIR_EXHAUSTED" | CONFIRMED-WITH-CORRECTION | Cause: the hatch reply hit the 16,384-token output cap (`finish_reason=length`) |
| F1.12 | Neither turn handed off before the deadline | CONFIRMED | No `handoff_to_user` / text exit in any S10 call |
| F1.13 | Bottom-line table "2/2 failed" | CONFIRMED-WITH-CORRECTION | n = 1 session (two turns of the same empty session), not two trials |
| F7.1 | Planner system message byte-identical to the tool-loop skill, sha `2e4c56762a74` | CONFIRMED-WITH-CORRECTION | Identical to `render_system_prompt(data_dir)` (service.py:751); equals `(None)` only with no deployment overlay. Composition note in §F7.1 |
| F7.2 | 23 unoffered tools, 109 mentions | CONFIRMED | Reproduced exactly with a controlled matcher. 106/109 are in the freeform overlay. Further mentions exist outside the system message (§Missed M4) |
| F7.3 | `emit_pipeline_proposal` never named in system message; bridge is "a single sentence in the last user message" | CONFIRMED-WITH-CORRECTION | 0 mentions; the bridge is the `instruction` field of messages[2] (pipeline_planner.py:1943), the last message only on the first call; also in the tool's own description |
| F7.4 | 66/110 (60%) planner calls reason about the missing palette | OVERSTATED (pattern-sensitive) | Re-derived 28% (strict) to 64% (broad). The broad count is almost entirely the token "proposal-only", which is the skill's own vocabulary |
| F7.5 | F7 is the "root cause behind F1" | OVERSTATED | F1 is a schema/custody gap. A perfect planner prompt still cannot bind a second blob; R1 cannot fix F1 |
| X.1 | Cross-check / Method: "escape hatch 3 tools" | REFUTED | The production hatch sends 23 tools. The 3-tool set is a boot-probe request plus a dead manifest option (§X.1) |
| X.2 | Live: 0 calls to unoffered tools | CONFIRMED | EXECUTED over 344 tool calls (lane + evals runs); instrument controlled |
| X.3 | Advisor system message = tool-loop prompt + 4,124-char suffix | CONFIRMED-WITH-CORRECTION | 62 calls at 139,987 chars; one more advisor call at 138,254 chars (another suffix) |

Counts: CONFIRMED 8, CONFIRMED-WITH-CORRECTION 10, OVERSTATED 2, REFUTED 1, UNVERIFIABLE 0.

---

## F1 details

### F1.4 Is multi-file really inexpressible on the planner? (EXECUTED + READ)

The planner can author only through `emit_pipeline_proposal(pipeline=canonical_set_pipeline_schema())`
(pipeline_planner.py:1584-1609). I checked every route by which a second uploaded file could reach a pipeline:

| Route | Available to the planner? | Evidence |
|---|---|---|
| Singular `source.blob_id` / `source.inline_blob` | Yes, but **one** blob only. `oneOf` forbids `source` together with `sources` | EXECUTED: schema `oneOf` printed; sessions.py:1059-1101 resolves via `_resolve_source_blob(source_name="source")` |
| Named `sources.<n>.blob_id` / `inline_blob` | No: `additionalProperties: false` without those keys | sessions.py:2184-2210. Live rejection (S10 T1 seq 10, 12): `canonical_schema … sources/item additionalProperties` |
| Named source `options.path` | No in practice. Only `{data_dir}/blobs/<session_id>/…` is allowed. The session id and storage filename are never disclosed (`get_blob_metadata` returns id/filename/mime/size/hash/status only). Guessing would breach custody anyway | paths.py:76-85, tools/_common.py:2331-2361; live `source_path_policy_invalid` ×4 (T1 seq 11, T2 seq 19/20) |
| Reviewed-source `blob:` path tokens | No: these are only rehydrated from a server `ReviewedSourceAuthority` hash match, not authored | sessions.py:1302-1323, 799-824 |
| `blob_rows` with an authored `blobs` list | No: the key is resolver-owned and `set_pipeline` rejects it | sources.py:757-777; applied at sessions.py:~1434 (`_reject_manual_source_blobs`) |
| `blob_rows` + `blob_csv_expand` | Would work (`blob_csv_expand` is in the digest; S10 msg[1] has it ×4), but it needs the `blobs` binding above | blob_csv_expand.py:1-60; set_source_from_blobs → blob_rows (sources.py:1618-1665) |
| `create_blob`, `wire_blob_inline_ref`, `set_source_from_blob(s)` | Not on the palette | EXECUTED palette listing |

The models found all of this themselves. DeepSeek at T1 seq 8/9/11 and T2 seq 17/18, and glm at T1 seq 13 / T2 seq 21,
each considered and rejected the `blob_rows`+`blob_csv_expand` and path routes.

Note for the README wording: the `blob_rows` hint is not a red herring. `set_source_from_blobs` → `blob_rows` →
`blob_csv_expand` is a genuine tool-loop route for "combine two CSVs". The tool loop also has
`set_source_from_blob(source_name=…)` ×2 (sources.py:1671-1690).

### F1.5 / F1.10: T1 call count and the hatch's wall clock (EXECUTED)

`S10/turn01/llm_calls.json` holds 9 completed calls: seq 5-12 on DeepSeek (0.9-25.3 s) and seq 13 on glm-5.3
(start 20:44:10, end 20:45:19, 69.2 s). Seq 13 returned three discovery reads (`get_blob_content` ×2,
`explain_validation_error`). `LIVE/instances/lv4/web.log` then shows:

- planner_attempt 9 `discovery_executed` at 20:45:19.39;
- `LiteLLM completion() model= z-ai/glm-5.3` at 20:45:19.53;
- `composer.planner_summary final_outcome=BaseExceptionGroup` at 20:46:31.28, which is the operation deadline
  (`deadline_at 20:46:31Z`).

The capture callback records only completed calls, so this 10th request was in flight for about 72 s and is not
in the capture.

**Correction:** "The planner made 10 provider requests over 241.6 s (9 completed and captured). The escape hatch
used two: the first took 69 s and asked for three reads; the second was still running after about 72 s when the
240 s operation deadline cancelled it (504)."

### F1.6: who said "That tool isn't available in this palette" (EXECUTED)

The quoted sentence is in the reasoning of T1 seq 13, model `z-ai/glm-5.3` (the hatch). The primary model said the
same thing at T1 seq 8: "No create_blob, no wire_blob_inline_ref, no set_source_from_blobs."

**Correction:** attribute the quote to the escape-hatch (advisor-model) call, or cite the DeepSeek seq 8 sentence.

### F1.8 / F1.11: why T2 reached the hatch, and why the hatch failed (EXECUTED + READ)

`web.log` (lv4) for T2:

- attempt 5 `outcome=truncated`. Seq 18, `finish_reason=length`, 16,384 completion tokens, 70k chars of reasoning,
  55.6 s.
- attempt 6 rejected (`source_path_policy_invalid`).
- attempt 7 rejected (`source_path_policy_invalid`, `repeated_fingerprint=True`), `led_to=hatch`.

The truncation consumed a repair slot (pipeline_planner.py:4337-4342). The hatch call (seq 21, 70.6 s) also ended
`finish_reason=length` at 16,384 tokens with 75k chars of reasoning and no tool call. The hatch-truncated branch
(pipeline_planner.py:4329-4336) then re-raised the original `REPAIR_EXHAUSTED`. The cap is
`composer_planner_max_completion_tokens=16_384` (web/config.py:379), and hatch turns run at candidate (high) effort.
These are the only 2 `length` finishes among the 110 planner-classified calls.

**Correction:** "T2: one planner reply was truncated at the output cap and two path-based proposals were rejected,
which spent the repair budget. The hatch's own reply was then truncated at the 16,384-token cap while it debated
declining, so the original REPAIR_EXHAUSTED stood."

### F1.13: sample size

"2/2 failed" counts two turns of one session; T2 ran on the still-empty session. Say "1 session (2 turns)". The
severity still holds: the gap is a deterministic schema/custody limit, not a stochastic model failure.

---

## F7 details

### F7.1 Byte identity and what the text actually is (EXECUTED)

Hashing `messages[0]` across all captures (lane + `evals/…/runs`):

- 124 DeepSeek planner calls and all 4 hatch calls are 135,863 chars, sha `2e4c56762a74`, identical to
  `render_system_prompt(None)` today.
- 111 loop calls carry the same hash.

Production passes `render_system_prompt(self._data_dir)` (service.py:751 → planning_application.py:1077 →
pipeline_planner.py:3495). That appends `{data_dir}/skills/pipeline_composer.md` when the file exists
(prompts.py:96-100). The audit instances had none, so "identical to the tool-loop prompt" is right in general, and
"identical to `render_system_prompt(None)`" holds only for a deployment without an overlay.

Composition (EXECUTED split at the `# Freeform Pipeline Composer Interaction Policy` heading):

| Part | Chars | Unoffered-tool mentions | Description |
|---|---|---|---|
| Canonical capability core (`pipeline_capabilities.md`) | 32,772 | 3 | Shared by design: "Every authoring surface receives these exact bytes before its interaction rules" (SP L4-7) |
| Freeform Pipeline Composer Interaction Policy | 103,091 | 106 | Starts at SP L511; this is the tool-loop policy |

So the planner gets the shared core, correctly, **plus the freeform/tool-loop overlay**, which is the defect.
"Full tool-loop skill" is accurate, but the fix seam already exists (see R1).

### F7.2 Recount (EXECUTED, controlled)

Matcher: `(?<![A-Za-z0-9_])NAME(?![A-Za-z0-9_])` over SP, for `set(composer_loop_tool_definitions) - set(planner_tool_definitions)`.

Controls:

- positive `set_pipeline` = 34;
- negative `emit_pipeline_proposal` = 0;
- negative `nonexistent_tool_xyz` = 0;
- boundary string `"build_set_pipeline_candidate x set_pipeline_y set_pipeline."` → 1 (expected 1);
- naive substring count for `set_pipeline` = 35, which shows the boundary is doing work.

Result: 23 unoffered tools, all 23 mentioned, 109 mentions; per-tool numbers identical to the cross-check.

Nuance worth stating:

- Of the 3 core mentions, `set_pipeline` at SP L14 names the **document shape**. The planner's own instruction uses
  the same name ("canonical set_pipeline argument object", pipeline_planner.py:1943-1951).
- `request_interpretation_review` at SP L156 sits inside an explicit palette conditional ("Where your palette
  carries … where it does not, the staged requirement rides in … the terminal proposal").

### F7.3

Zero mentions of `emit_pipeline_proposal` in SP: CONFIRMED. By design, the core says "your interaction rules and
per-request instructions name yours" (SP L14-16), and the per-request part is `PLANNER_TERMINAL_INSTRUCTION`
(pipeline_planner.py:1943), placed in `provider_request["instruction"]` (pipeline_planner.py:3485). That is
messages[2], which is the last message only on the first call. The tool's own description also states the protocol
(pipeline_planner.py:1598). The finding stands: the 103k-char overlay never names the planner's terminal.

### F7.4 The 60%: re-derivation and sensitivity (EXECUTED, controlled)

Population: planner-classified calls in `LIVE/sessions` (palette contains `emit_pipeline_proposal` and not
`set_pipeline`) = **110**, the same as the audit, but **4 of them are hatch calls on glm-5.3** (106 primary). All
patterns below matched both positive controls and neither negative control. Positives: "On proposal-only surface,
there is no request_interpretation_review tool advertised." and "I don't have set_source_from_blobs in this
palette…". Negatives: "The csv source is available in the digest and list_blobs returned two blobs." and "I will
emit the proposal now…".

| Pattern (per sentence of `reasoning_content`) | Planner calls hit | Loop baseline |
|---|---|---|
| S strict: names an unoffered tool AND a negation AND an availability word | **31/110 = 28%** (28/106 primary) | 3/97 |
| M: S, or palette words (`proposal-only`, `palette`, `not offered/advertised`, `no such tool`…) | **70/110 = 64%** (66/106 primary) | 12/97 |
| B: any sentence naming any unoffered tool | 47/110 = 43% | n/a (offered there) |
| B2: B excluding `set_pipeline` | 39/110 = 35% | n/a |

Sensitivity:

- 67/110 calls contain "proposal-only"/"proposal only". Of the 70 M hits, only 3 lack that token, so the audit's 60%
  is essentially a count of that token.
- "proposal-only" is the **skill's and schema's own vocabulary**: SP L47, L637, L713, L1853, and the
  `interpretation_requirements` schema note (tools/_common.py:177-193). Typical M-only hits are the model correctly
  applying the shared text's surface conditional, for example s09 seq 17: "On a proposal-only surface, include the
  pending requirements in … the complete proposal instead. Good — I stage them in options."
- In the strict set, the tools named as absent (calls per tool) are `request_interpretation_review` 20,
  `set_source_from_blobs` 7, `request_advisor_hint` 4, `set_pipeline` 4, and others at 1-2.
- Excluding s10: strict 24/93, M 58/93.

Real confusion does exist. The audit's s01 T1 seq 1 quote is genuine: the review-ownership matrix says
`invented_source` is "surfaced by YOU", which requires a tool the planner lacks. But the honest numbers are:

- **28%** of planner calls explicitly reason that a taught tool is missing;
- **about 60%** spend reasoning locating their surface in a shared text.

**Correction:** "31 of 110 planner-classified calls (28%; 4 of the 110 are escape-hatch calls on the advisor model)
explicitly reason that a taught tool is not on their palette; 67 (61%) reason about being on the 'proposal-only'
surface, a term the shared skill itself uses. The rate depends heavily on the pattern."

### F7.5 "Root cause behind F1"

This is OVERSTATED. Even with a planner-specific overlay, the canonical schema still allows only one blob-bound
source (§F1.4). R1 can shorten the dead end (the planner would stop hunting for `set_source_from_blobs`), but only
R4 makes s10 buildable. The remediation table lists R1 as fixing "F7, F1"; change it to "F7 (and shortens F1's
dead end)".

### X.1 "Escape hatch: 3 tools" is wrong

The production hatch is `_engage_escape_hatch` (pipeline_planner.py:4201-4243), and its turns call
`planner_tool_definitions(discovery_policy, …)` (pipeline_planner.py:4275). That is the same 23 tools, and all 4
captured hatch calls show 23.

The audit's 3-tool "escape hatch" comes from `evals/composer-live-audit/build_register.py:42`
(`recovery_tool_definitions + planner_terminal_tool_definition`), which mirrors two stale artefacts:

- **boot_probe.py:201-228:** the `hatch_terminal` probe sends 3 tools, and its comment claims the 16-token cap is
  "the one deliberate difference from a hatch turn's request". That is false.
- **capability_skill.py:193-219:** `tool_surface="terminal_only"` for an "escape-hatch overtime turn" that has no
  caller. The only call passes `"full"` (pipeline_planner.py:3634).

The register, README Method §1 ("the 3 escape-hatch tools") and the cross-check's "escape hatch 3" should all say
the hatch = advisor model + the 23-tool planner palette + the planner system message. The README's F1 text already
says this correctly.

---

## Remediation comments

### R1 (per-surface skill)

- **Direction:** right, and invariant-safe (prompt text only).
- **Smaller, safer mechanism than tagging and stripping loop-only passages.** The seam already exists:
  `render_with_pipeline_capabilities(interaction_skill)` "prepend[s] the exact capability-core bytes to one
  interaction overlay" (capability_skill.py:155-159). The planner manifest only requires the first system message
  to *start with* the core (capability_skill.py:204-209). Write a planner interaction overlay and render
  `core + planner_overlay` for planner and hatch turns. Leave the loop untouched, so there is no strip logic to keep
  correct.
- **Risk to manage:** the planner-relevant parts of the freeform policy (review staging, Reply Register,
  termination, decline protocol) must not drift between two overlays. E3 is the guard.
- **Missed coupling 1, advisor:** the advisor's system message is `composer_skill_text + trigger instructions`
  (advisor_checkpoint.py:454). On planner turns it should see the text the planner actually received, or it will
  judge planner output against the freeform policy.
- **Missed coupling 2, provenance:** `composer_skill_hash` is one per-process hash (service.py:752; planner
  pipeline_planner.py:3392). It then flows into blob and proposal provenance, so per-surface text needs a per-surface
  hash.
- **Missed coupling 3, deployment overlay:** the operator skill (prompts.py:97-99) must still be appended on the
  planner.
- **Proof target "109 → 0" is unattainable as written.** The shared core legitimately names `set_pipeline` as the
  document shape (SP L14), as does the planner's own instruction. Target 0 in the planner overlay, plus an explicit
  allowlist of the core's 3 sanctioned mentions; or rename the shape to "canonical pipeline document" in both.
- **Second surface left out:** the tool schemas carry the same teaching (M4). Surface-conditional prose there is
  acceptable, since it is shared with the loop.

### R4 (multi-file on the planner)

**(a) is safe against blob custody, provided it reuses the existing resolver.** Each named `sources.<n>.blob_id`
must go through `_resolve_source_blob(..., source_name=<n>, rejection_operation=CANDIDATE,
existing_options=state.sources.get(n))`. That is exactly how singular `source.blob_id` is settled
(sessions.py:1082-1101), and the tool loop already does the same for named sources via
`set_source_from_blob(source_name=…)` (sources.py:797-811, 1671-1690). The resolver already:

- rejects manual `blob_ref`/`source_authoring`/`blobs`;
- checks session ownership, ready status and fresh content hash (`_sync_get_blob(engine, blob_id, session_id)`);
- derives `path` and `blob_ref` from records;
- stages LLM-authored review custody per source name.

No new custody primitive is introduced. Conditions:

1. Keep `inline_blob` out of named sources. It creates blobs and needs the storage and review bounds of the
   singular path.
2. Define precedence against the reviewed-source branch (sessions.py:1302-1323): reject `blob_id` on a source that
   hash-matches a reviewed binding, rather than silently preferring either.
3. Reject `blob_id` together with `options.path`/`file`, as singular does with `inline_blob`
   (`source_binding_ambiguous`).
4. Expect whole-tree churn: the canonical schema is shared with the loop's `set_pipeline`. The capability core's
   pinned field inventory (capability_skill.py:139-152, `canonical-field-inventory` block) and the wire census must
   be updated.
5. Prove the LLM-authored-blob review path for a named component id (`source:q1`) with a focused test. This is
   READ-inferred, not executed.
6. Prove the combining topology. Two named csv sources, each followed by a `value_transform` that sets `quarter`,
   writing to one sink, was never exercised in the battery. Run s10 against it.

**(b)** A planner `blobs` binding equivalent to `set_source_from_blobs`, feeding `blob_rows` → `blob_csv_expand`, is
also valid but heavier: new resolver plumbing on the terminal, and the plural path refuses LLM-authored blobs
(sources.py:1424-1438).

**(c) Smaller interim, missed by the audit:**

- Make the rejection say what is wrong. Today the planner gets
  `{"path":"sources/item","rule":"additionalProperties"}` with no property name (S10 T1 seq 11/12 tool feedback).
  A specific message ("named sources cannot bind uploaded files on this surface; only `source.blob_id` can") is
  server-side validation text and invariant-safe. It would let the model hand off after the first rejection.
- Combine that with the hand-off teaching.

**No-progress guard / deadline:**

- The planner deadline equals the operation deadline (service.py:1245, pipeline_planner.py:3393-3395). The
  operation owner cancels first (T1 `final_outcome=BaseExceptionGroup`), so no planner-owned TIMEOUT disposition or
  user message ever forms. The guard needs a reserve below the operation deadline.
- Any server-authored hand-off text must carry no graph, rejected candidates included, to respect invariant 1.
- **Also add an output-cap rule** (missed by the audit). In T2 both the primary and the hatch hit 16,384 completion
  tokens while reasoning about this dead end. Either keep a truncation from costing a repair slot when no tool call
  was produced, or give the hatch a lower reasoning effort or larger cap.

### R8 (plugin prose names only available plugins)

- Invariant-safe: render-time annotation or redaction, no structure.
- It must apply to every projection, not only the digest: the digest `not_for` in `authoring_aids`, the
  `composer_hints` returned by `get_plugin_schema`, and `get_plugin_assistance`. In s10 the planner read the blob_rows
  hint through all of these.
- As written, R8 covers plugin names, but blob_rows.py:176 and the blob_rows `not_for` name a **tool**
  (`set_source_from_blobs`), which only R1/R4 address.
- Rewriting free prose by regex is fragile. Prefer structured cross-references on plugin metadata, or one digest-level
  note listing the plugin ids named in prose that are not enabled, computed with a controlled id matcher.

### E3 (per-surface cross-check as a CI test)

- Good, but its scope must be **the whole outbound request per surface**, not only the system message. Today:
  - the planner tool definitions carry 11 unoffered-tool mentions, 9 of them inside `emit_pipeline_proposal`'s own
    schema (`source.blob_id` "resolved exactly like set_source_from_blob", the `interpretation_requirements` notes);
  - `authoring_aids` carries 8 (S10 msg[1]);
  - `PLANNER_TERMINAL_INSTRUCTION` carries 1.
- Model the hatch correctly (advisor model, 23 tools).
- Allowlist the core's sanctioned conditional mentions instead of asserting `= 0` globally.
- Require a controlled positive and negative for the matcher.
- The existing gates (`generate_skill_inventory.py --check`, `composer_wire_census.py`) do not cover any of this.

### P1

- Valid for the planner. The overlay is 103,091 chars; at the ~3.67 chars/token implied by S10 seq 5 (192,794 chars
  → 52,529 prompt tokens), that is roughly 28k tokens, about half the first-call prompt (INFERRED estimate).
- With an 88% planner cache hit the cost saving is mostly on cached-token pricing; the reasoning saving is the
  bigger win.
- The **advisor** half of P1 has no mechanism: R1 explicitly keeps today's text outside the planner, and the advisor
  reuses `composer_skill_text`. Trimming the advisor's copy also weakens the basis it reviews against. Drop the
  advisor claim or specify it.

### P5

- Valid and needed, but see the R4 note: it needs a reserve below the operation deadline, and it should also bound
  the hatch. In T1 the hatch alone spent about 141 s across 2 calls.

---

## Missed issues (in slice)

- **M1. Boot probe and manifest describe a hatch that no longer exists.** boot_probe.py:201-228 probes the hatch
  route with 3 tools and claims parity with the hatch turn, but production hatch turns send 23 tools
  (pipeline_planner.py:4275). So the hatch's discovery-tool schemas are never probed on the hatch dialect or
  endpoint. `tool_surface="terminal_only"` (capability_skill.py:193-219) is dead. Severity LOW-MEDIUM: the probe's
  coverage claim is false.
- **M2. Output-cap truncation in the multi-file dead end.** The only 2 `finish_reason=length` among 110 planner calls
  are S10 T2 seq 18 (primary) and seq 21 (hatch), at 16,384 tokens each. A truncation consumes a repair slot
  (pipeline_planner.py:4337) and ends the hatch outright (4329-4336). This is part of why T2 never handed off.
- **M3. Opaque rejection for the exact multi-file mistake.** See R4(c): `sources/item additionalProperties` gives no
  property name. R12 mentions the general problem; this is the specific instance that would have shortened F1.
- **M4. Unoffered-tool teaching outside the system message.** 11 mentions in planner tool definitions (both dialects
  identical), 8 in `authoring_aids`, 1 in `PLANNER_TERMINAL_INSTRUCTION`. The cross-check and E3 as written cover
  only the system message. EXECUTED with the same controlled matcher.
- **M5. Population mixing.** The audit's "110 planner calls" (and the performance table's first-turn row) include
  the 4 hatch calls on the advisor model. The classifier in `mine_sessions.py:28-33` and `build_register.py:114-122`
  cannot tell them apart. Use `model` to split.
