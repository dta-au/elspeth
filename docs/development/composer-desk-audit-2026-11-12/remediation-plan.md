# Composer remediation plan (from the 2026-11-12 desk audit)

Findings (F*) are in [README.md](README.md); the tool inventory is in
[tool-call-register.md](tool-call-register.md). Items are grouped as fixes
(R), performance (P) and enhancements (E), then sequenced into work packages.
Every item names how to prove it landed: re-run the live battery in
`evals/composer-live-audit/` and the static per-surface cross-check.

Composer invariants apply to every item. The LLM authors all structure, and
there are no tutorial-only paths. None of the items below routes around the
provider or synthesises structure on the server. R2 and R6 add server-side
evidence or validation only.

## Fixes

| ID | Fixes | Change | Size | Proof |
|---|---|---|---|---|
| R1 | F7, F1 | Render the system prompt **per surface**. The planner gets planner text: `emit_pipeline_proposal` named as its terminal; "stage the requirement in node options" instead of `request_interpretation_review`; no mutation-tool names. The loop keeps today's text. Mechanism: tag loop-only passages in `pipeline_composer.md`, as the autogen and `taught:` blocks already do, and strip them in the planner renderer. Plugin hints (`blob_rows.py:176`) need a surface-neutral wording or a per-surface variant. | M | Per-surface cross-check: planner mentions of unoffered tools go from 109 to 0, and `emit_pipeline_proposal` is named. Battery: planner reasoning about the missing palette falls well below 60%. |
| R2 | F2 | Give the advisor the capability facts the composer answered from. Thread `plugin_snapshot` into `TurnEvidence.review_candidate` from its four call sites. Add a "current deployment capabilities" record (available ids, prohibited ids, usable profile aliases), bound by snapshot hash and reusing `advisor_abandonment.py:109-129`. Add one sentence to the END rubric naming it as the source of the composer's digest. | S–M | s04 T2, p01 T2 and p01 T3 admitted on 5 of 5 repeats each. A hallucinated capability ("yes, there is a SharePoint source") still gets WITHHOLD. |
| R3 | F2, F8 | User-facing copy for withheld and receipt states. Use `advisor_policy.py:451-455`-style wording for withheld replies, and keep the saved-scope receipt out of chat or behind a details toggle. For informational turns with zero saved revisions, show no receipt. Fix the duplicate sentence on withheld tool-loop turns. | S | Screenshot review of s04 T2, s01 T7 and s09 T2. |
| R4 | F1 | Make multi-file sources expressible on the planner, or deliberately not. Options, in order of preference: (a) let named `sources` entries carry a session `blob_id`, settled by planner custody exactly like the singular `source.blob_id`; (b) give the planner terminal a `blobs` binding equivalent to `set_source_from_blobs`; (c) at minimum, teach the planner to hand off ("I'll set up the first file; ask me to add the second"). Separately, add a no-progress guard so a planner turn hands off before the wall-clock deadline instead of returning a 504. | M (a), S (c) | s10 builds in under 60 s, or hands off with a clear message within the turn. |
| R5 | F3 | Pass `baseline` into tool-loop dispatch: request-start state by default, and the last accepted or committed state when the user asks about "since I accepted". Reword `diff_baseline_unavailable` so it never says "load or create a session". | S | "What changed since the version I accepted?" lists the real change in r01–r03. |
| R6 | F4 | **Needs operator decision.** The credential guard on reasoning content hard-fails the turn. Options: (a) when the hit is in reasoning only (not tool arguments or visible content), drop the reasoning from persistence and replay, record the event, and continue; (b) re-ask the model once with a note not to reproduce credential-shaped strings; (c) keep the hard fail but replace the user copy, which wrongly implies the user supplied a credential. Option (a) keeps the control on everything that is persisted or executed. | S–M | s06 T1 builds; a real credential pasted by the user is still refused. |
| R7 | F5 | Make the skill's model-binding paragraph (`pipeline_composer.md` ~L892-903, the `llm_model_choice` row at ~L1236) conditional on the served schema exposing `model`. For the web profile-only projection, teach "profiles only; to change models, ask your operator". Filter `list_models` to the deployment's profiles (alias to provider/model where disclosure policy allows), or remove it from the web palette. | S | s02 T4 and p01 T1 replies stop citing the litellm catalog. |
| R8 | F6 | Plugin `not_for`, purpose and hint text should name only plugins that are available in the current policy snapshot. Otherwise say "an operator can enable X". Apply at digest render time. | S | c01 reasoning no longer finds a contradiction. |
| R9 | F9 | In the advisor-repair instruction, add: "The user has not seen your previous draft. Write the reply as the first and only message; do not call it a correction." | XS | s02 T3 and s20 T2 replies stop opening with "Correction". |
| R10 | F10 | Reword `_ALL_INFORMATION_GAPS_CLOSED_NOTICE` as information: "The schemas you asked for are now in context. When you have what you need, emit the proposal; otherwise continue discovery or reply in text." | XS | Battery: no reasoning conflicts with the notice. |
| R11 | F11, F15 | `report_assemble` declares `output_semantics()` (STR, unconstrained framing). Fix the `document` sink example, whose `schema.mode: fixed` errors after `report_assemble`. Correct the always-authorized list in `docs/reference/environment-variables.md` (9 listed vs 14 in `plugin_policy/compiler.py`). | S | s08 / s14 validate with no high warnings. |
| R14 | F16 | In `composer_preflight.reuse_or_recompute_runtime_preflight`, return a computed (cached per version) runtime preflight for every non-empty state, not only invalid or blob-bearing ones. The advisor gate then always has a real base: a green pipeline takes the completion-only `advisor_signoff_pending_validation` path, never the "unverified" one. In the UI copy, replace "after your next pipeline change" with the actual next step, and de-duplicate the repeated sentence. | S | s03 T4 and s09 T2 replays: no "not re-verified" text; a valid graph with an advisor FLAG shows completion-pending only. A unit test pins a valid, unchanged, no-preview turn to a non-`None` preflight. |
| R15 | F17 | Reply Register addition: questions about checks, readiness or approval state are answered with the user's next action in product terms. Never show tool-call JSON or ask the user to run a tool or file a defect. Add the case to the advisor END rubric so a reply that does either is flagged. | XS | Ask "why can't I share this?" on an advisor-blocked session: the reply names the step and action, with no tool names. |
| R16 | F18 | **Decision needed.** Tutorial dead end after "Apply" adds a failure-retention output. Options: (a) let `_tutorial_launch_blocker` (`tutorial_service.py:154-215`) admit one additional `sink:json` that receives only failure routes; (b) hide suggestion Apply in `TutorialFreeformShell` (UI-only; composer untouched); (c) make the 409 `tutorial_plugin_set` detail name the extra output and the fix. Do (c) regardless. Also make the "Every card acknowledged… Select Run pipeline" chat line say "Continue to Run" inside the tutorial. | S | Browser walk: brief → Accept → Apply → acknowledge all cards → Continue to Run either proceeds (a) or the Apply control is absent (b); the 409 text names `failed_rows` (c). |
| R13 | F14 | At staging, reject a `vague_term` wired at node level on an `llm` node where no query falls back to the node-level template. Alternatively, make the resolver write into every query template that carries the slot. Add one teaching line to the multi-query section. | S | In-process repro from the wiring report: either rejected at `set_pipeline`, or `accepted_in_prompt=True` for every query. |
| R12 | F12 | Tool-result legibility. For discovery tools, put the tool's own error first and omit whole-pipeline validation. Make sibling argument names agree (`get_plugin_assistance.plugin_name` vs `get_plugin_schema.name`), or accept both with one deprecated. Name the offending property in `canonical_schema` rejections ("unexpected key `nodes` under `source`"). | S | Proposal rejection rate below 16 of 53. |

## Performance

| ID | Change | Expected effect |
|---|---|---|
| P1 | Per-surface prompts (R1) also shrink the planner and advisor system text. Today all three roles receive the 136k-char skill. | Fewer prompt tokens on the planner and advisor; less reasoning spent working out the palette. |
| P2 | Advisor caching: only 45% of advisor prompt tokens hit the cache versus about 90% on the primary. Put the stable skill prefix first with a cache marker; put per-turn evidence last. | Lower advisor latency and cost. |
| P3 | Skip or lighten the END advisor on turns with no saved effects and no capability claims ("Thanks", "Explain this"), or use a smaller advisor budget there. Leave mutation turns untouched. Weigh this against the advisor's real catches (F9). **Decision needed.** | Saves 1 call (~4 s, ~55k tokens) per conversational turn. |
| P4 | Reduce proposal rejections (R12) and remove the "emit now" pressure (R10). 16 of 53 proposals were rejected, each a full planner call of 8–25 s. | Fewer planner calls; first-turn p50 below 27 s. |
| P5 | Wall-clock-aware planner exit (in R4): hand off before `composer_timeout_seconds` instead of letting the operation die at 504. | No 240 s spinners. |
| P6 | Address the startup warning `composer_turn_budget_underfunded` (34 configured turns, about 16 fundable at 240 s). Either size the budgets to the timeout or raise the timeout for the demo profile. | Budgets that can actually be spent. |

## Enhancements

| ID | Change |
|---|---|
| E1 | First-turn reply says what was built. Keep the verdict constant and prepend a short model-written summary that the advisor can check, as tool-loop replies already do. |
| E2 | Promote the tool-call register to a generated, gated artifact (next section). |
| E3 | Add the per-surface teaching cross-check as a CI test: for each surface, prompt mentions of tools that surface is not offered = 0, and every offered tool is taught. This is the exact "the skill says X but I don't have X" guard. |
| E4 | Add the live battery's demo-shaped scenarios (multi-file, capability questions, "what changed", security screening, "use GPT-4o") to `evals/composer-battery/corpus.md`. The current corpus covers none of them. |
| E5 | A "Run it" hint. The composer cannot start a run and says so in three different ways (r01–r03). Teach one sentence naming the UI control. |
| E6 | Refresh `docs/reference/composer-tools.md`. It documents 31 of 46 live tools, has no planner surface, and its budget table hardcodes 10/15 turns, while the budgets are required deployment settings. This audit adds a pointer to the register; the full rewrite should follow R1. |

## Tool-call register: plan

The register in [tool-call-register.md](tool-call-register.md) is a snapshot.
To keep it true:

1. **Generator.** Move `evals/composer-live-audit/build_register.py` and
   `render_register.py` to `scripts/cicd/composer_tool_register.py`. They
   already read only live objects: `composer_loop_tool_definitions`,
   `planner_tool_definitions`, recovery and terminal definitions,
   `get_tool_definitions`, `_registry`, the redaction `MANIFEST` and
   `render_system_prompt`.
2. **Columns to add.**
   - A per-surface rendered prompt once R1 exists, so mention counts are per
     surface.
   - Budget class per tool: discovery, composition or free.
   - Result shape: the response TypedDict and redaction admitted keys, from
     `composer_wire_census`.
   - FRONTEND projection.
3. **Gate.** `--check` regenerates and diffs the static columns. Live columns
   (call counts, failure samples) come from the battery and are excluded from
   the diff.
4. **Ownership.** Any change to a `TOOLS_IN_MODULE` tuple, a tool
   description or a skill passage regenerates the register in the same
   commit, as `generate_skill_inventory.py` already does for the inventory
   block.
5. **Live join.** `evals/composer-live-audit/run_battery.sh` produces the
   captures. Re-run before each release candidate and before any external
   demo.

## Work packages and order

| WP | Contents | When |
|---|---|---|
| WP0: demo config (no code) | Runbook below | Before the demo |
| WP1: stop the visible failures | R16(c), R2, R3, R5, R9, R10, R14, R15 | Before the demo if possible; each is small and independent |
| WP2: planner surface | R1 (+P1), R4, R8, E3 | After the demo; R1 first because R4 and R8 build on it |
| WP3: model binding and guard | R6 (after decision), R7 | After WP1 |
| WP3b: integrity | R13 | With WP1; it is a correctness bug in approved review content, independent of the demo |
| WP4: legibility and performance | R11, R12, P2–P6, E1 | Ongoing |
| WP5: register and corpus | E2, E4, E6 | Alongside WP2 |

Each package ends with: focused unit tests, the whole-tree gates its inputs
touch (see CONTRIBUTING), the per-surface cross-check, and one live battery
run compared against this audit's turn table.

## Demo-day runbook (WP0, no code changes)

### Configuration

- **Prompt guard.** The default `ELSPETH_WEB__COMPOSER_PROMPT_GUARD_MODE=required`
  needs both Azure Prompt Shields mounts, otherwise
  `/api/system/status` reports `composer_available: false` ("required prompt
  guard configuration is missing for composer, advisor"). Confirm on the demo
  host.
- **Plugin allowlist.** Add the plugins the demo uses, at least
  `transform:type_coerce`, `transform:value_transform`, `transform:passthrough`,
  `transform:truncate`, `transform:keyword_filter`,
  `transform:report_assemble`, `sink:document`, and the batch plugins if
  aggregation is shown (F6). Restart, then check the
  `plugin_policy_readiness` rows.
- **LLM profile.** At least one `ELSPETH_WEB__LLM_PROFILES` entry plus
  `ELSPETH_WEB__DEFAULT_LLM_PROFILE`. With none, no LLM nodes can be authored.
- **Users.** The presenter account needs a deployment-wide `user` role,
  otherwise creating a session returns 403 `user_role_required`.
- **Timeout.** Consider raising `ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS` for the
  demo (P6). First turns ran p90 57 s, max 242 s.

### Preflight

1. Re-run `evals/composer-live-audit` scenarios s01, s02, s03, s05, s09 and
   s20 against the demo host with the demo models. Every turn should complete
   and `validate` should pass.
2. Pre-warm: one throwaway session per demo flow, so caches are hot.

### Prompts

- **Safe**, all validated live:
  - "Here's our feedback export … classify complaint / compliment /
    question … one-line summary … CSV" with one attached CSV;
  - "Also put the complaints in their own separate file";
  - "Change the threshold to 500";
  - "Explain what this pipeline does in two sentences";
  - "Why did you choose that design?";
  - "Undo that last change";
  - "What can you do?";
  - "Build me a pipeline." (it asks good questions);
  - "Is this safe to run on sensitive data?".
- **Avoid until fixed:**
  - attaching two or more files to an empty session (F1);
  - "Can you connect to X?" or "Can you read PDFs?" (F2, a coin flip);
  - "What changed since…?" (F3);
  - prompts that ask to screen for passwords or secrets (F4);
  - "Use GPT-4o" or "which models" (F5);
  - arithmetic without the allowlist above (F6).
- **Expect** every first build of made-up data to show interpretation review
  cards; that is by design.
- **Tutorial:** do **not** click "Apply" on the "Consider adding error
  routing to a retention output" suggestion (F18). Accept the proposal,
  acknowledge the cards, use View prompt then Approve on the prompt card,
  then Continue to Run. In the browser walk the build took about 40 s to the
  first card, and the tutorial needed 4 decisions. If "Continue to Run" says
  the pipeline "does not match the supported tutorial plugin set", ask the
  Composer to "Remove the failed_rows output and discard failures instead."
  (verified: one 28 s turn, then tutorial readiness returned 200), or exit
  the tutorial.
- **If the panel says "Completion advisory review did not clear"** on a valid
  pipeline, Run still works ("Advisory checks don't block Run"). Click Run.
  Do not ask the composer about the gate on stage (F16, F17). To clear it,
  ask for a small real change, or ask it to "check the pipeline is ready to
  run", which normally makes it call `preview_pipeline`.
