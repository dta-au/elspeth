# Battery round 6 — targeted re-test report of record

Date: 2026-08-07. Author: `claude-r6-battery`.
Acceptance pin: **`69c6ad4b5`** (release/0.7.2).
Stack: scenario A, `elspeth.aws.foundryside.dev` (ap-southeast-2), web TD `:6`.
Evidence: `ops-local/acceptance/r6-evidence/` (L1), `ops-local/acceptance/r6-battery/`
and `r6-battery-state/` (live drives, incl. per-session `/state`, `/state/yaml`
and full message transcripts).

## What this round is

Not a full battery. The operator's instruction on 2026-08-06 was: *"rather than
doing a full battery, once those fixes land I'll come back to you for a rapid
test of just those"*. So the corpus is the three priority tickets' live legs,
plus one mechanism the round's own configuration fix newly makes reachable:

| Leg | Ticket | Question |
|---|---|---|
| g08 ×3 | `elspeth-41bcaa882e` | row_union guarantees — also the `elspeth-902fc354b2` unblock |
| g04 ×2 | `elspeth-9d59c33480` | review byte-match loop gone, in-band, inside the old parity wall |
| g03 ×1 | `elspeth-09c91778f5` | envelope closure at the shipped 900/840 render |
| g11 ×1 | (new) `elspeth-ba6a8dff24` | the llm **source** mechanism, unauthorable in rounds 4 and 5 |

Every LLM-mediated fix took the two-level rule: a deterministic Level-1 check
off-stack before any compose was spent on Level 2.

## Deviation from the written protocol, and why

`2026-08-06-battery-round-6-targets.md` §Rapid re-test says *"Roll the service
to the parity TD (`:3`-equivalent, 270s) first"*. Round 6 ran the whole pass at
the **shipped 900/840 render** instead. The protocol's stated reason for that
line was that the stock 240 *"would confound g08/g04 timings with the envelope
question"* — 900/840 satisfies that just as well, and is strictly better
evidence for two reasons:

1. It is what the package now ships. `e0d78882e` took the raise branch, so
   900/840 *is* the parity render as of this pin; 270 no longer ships anywhere.
2. It yields **uncensored** durations. A g04 that finishes in 180s under an
   840s ceiling proves it would have fitted in 270; a g04 that dies at a 270s
   wall proves nothing about the review-loop fix. The "inside the parity wall"
   criterion therefore becomes a *measurement* rather than a survival test.

The round-5 comparison baseline for g03/g04 is arm **B**, which ran at exactly
840/900.

## Acceptance-pin gates

| Gate | Result |
|---|---|
| `pytest tests/ -n 12` | **38093 passed, 3 failed**, 27 skipped, 1 xfailed (12m39s) |
| `elspeth-lints check` | exit 0 |
| `wardline scan --fail-on ERROR --fail-on-inert …` | exit 0 (clean *and* non-inert) |

The pin is **not** green, and this report does not claim it is. All three
failures —
`test_composer_runtime_agreement.py::test_both_reject_strict_sink_typed_requirement_without_upstream_guarantee`,
`test_value_transform.py::test_output_schema_config_guarantees_configured_targets_for_dag_validation`,
`test_dag_scenario_production_path.py::test_b2_sequential_nested_rejects_incompatible_second_merge_schema` —
belong to **`elspeth-9615d6c75a`**, which is `fixing`, claimed by another
session (`claude`, claim expires 2026-08-08). That ticket's own description
anticipates the flip: *"Direction pins already exist: TestExtrasFirewallDirection
(DAG accepts today) … that test documents the flip procedure when this lands."*

They are not regressions. `69c6ad4b5` added a build-time Rule A mirror that
rejects a producer's guaranteed extras against a locked (`extra=forbid`)
consumer — a shape that previously built green and then killed every row at the
executor input preflight on row 1. The product moved in the safe direction and
the test expectations lag. They were left untouched: reconciling another
session's in-flight work mid-claim would have been the wrong call.

**Risk this raised, and how it was cleared before spending money:** that same
new rule could have rejected the g08 corpus, whose `results` sink is
`mode: fixed` over three fields while the chain also guarantees
`complaint_text`, with only `select_fields` (`field_mapper`, `select_only:
true`) narrowing it. The L1 harness was given an explicit third outcome for
this — NEW-BREAK — and it did not fire.

## Level 1 — deterministic, off-stack, with negative controls

Each L1 replays the artefact **round 5 actually authored** (`/state` capture)
through the current builder, and then through the round-5 pin `59cb6f75e` with
the *same harness and same fixture*. A harness that always passes proves
nothing; the control is what makes the result evidence.

### `elspeth-41bcaa882e` — row_union guarantees → **PREVENTION**

Fixture: `r5-preserve/elspeth-battery-r5-A/g08-s2/state.json`.

| Code | Result |
|---|---|
| Round 6 (`69c6ad4b5`) | **ACCEPTED** — `validate_edge_compatibility()` accepted every edge |
| Round 5 (`59cb6f75e`) | **REJECTED**, byte-identical to the round-5 live failure |

The control reproduced round 5's message exactly, including node ids:

```
Schema contract violation: edge 'row_union_stack_results_ab2bf46405d4' → 'transform_select_fields_f6fe0102d9aa'
  Consumer (field_mapper) requires fields: ['complaint_id', 'style_tag', 'summary']
  Producer (row_union:stack_results) guarantees: (none - dynamic schema)
  Missing fields: ['complaint_id', 'style_tag', 'summary']
```

and it arrived as a bare `GraphValidationError` — i.e. no structured suggestion
channel at all, which is defect #2 of the ticket observed directly rather than
inferred. Evidence: `r6-evidence/l1-41bcaa882e.txt`.

### `elspeth-b19dfe41fb` — prompt shield on a declared-int field → **FIXED**

Fixture: `r5-preserve/elspeth-battery-r5-B/g11/state.json` — the artefact that
passed `/validate` in round 5 and then killed 100% of rows.

| Code | Result |
|---|---|
| Round 6 | **REJECTED at build time** |
| Round 5 | **ACCEPTED** — exactly why round 5 saw it validate and then quarantine every row |

The new message names the field, its declared type, the consequence and two
concrete fixes:

> Transform 'aws_bedrock_prompt_shield' … scans input fields that must be text,
> but its upstream … declares them non-string: 'sentence_num' is declared int.
> The transform fails closed on any non-string value … so every row from this
> producer is quarantined and the pipeline fails on the first row.

Evidence: `r6-evidence/l1-b19dfe41fb.txt`.

### `elspeth-9d59c33480` — review byte-match → **FIXED at the contract the model sees**

| Code | `required` on `request_interpretation_review` |
|---|---|
| Round 6 | `['affected_node_id', 'kind', 'user_term']` — `llm_draft` **optional** |
| Round 5 | `['affected_node_id', 'kind', 'llm_draft', 'user_term']` — **required** |

Round 6's description actively instructs omission: *"OMIT this when the review
site already carries a staged interpretation_requirements draft (the normal
case): the server resolves the staged draft verbatim, which is more reliable
than re-transmitting multi-line text."* Evidence:
`r6-evidence/l1-9d59c33480.txt`.

### `elspeth-155947ca47` — persisted `is_valid` vs preflight

Round 5's own preserved evidence contains the defect: `g08-s2/state.json` says
`is_valid: True` at version 9 while `g08-s2/validate.json` says `is_valid:
False` for that same version. Every round-6 live sample is checked for the same
divergence (see the per-graph table).

## Configuration defect found before the live legs

`source:llm` ships and is discoverable (`discover_all_plugins()` lists it) but
is absent from the AWS ECS module's `default_plugin_allowlist`
(`modules/scenario/locals.tf`), and was therefore absent from this stack. **The
llm source was unauthorable**, on this stack and on any cold install.

This **retracts** the g11 verdicts of rounds 4 and 5. Both recorded the
mechanism as unsampled because *"the composer authored around it"*. It did not
author around it — it could not reach it. Round 5's g11 artefact shows the
workaround directly: a CSV of sentence *numbers* plus an llm **transform** to
synthesise the text it could not source. Filed as `elspeth-ba6a8dff24`;
whether the **shipped** default should authorise it is left to the operator,
because an llm source is author-controlled model invocation at the source
position.

For round 6 the acceptance stack was corrected: `source:llm` added to the
scenario-A tfvars and to TD `:6`, with the plugin-policy binding digest
recomputed (see below). `GET /api/catalog/sources` now returns
`['aws_s3','csv','json','llm','null','text']`.

## Stack changes made for this round

| Change | Detail |
|---|---|
| ALB idle timeout | 300 → **900**, targeted apply (`-target=module.scenario.aws_lb.web`), plan showed exactly 1 change. **Verified on the ALB itself**, not via the env var that claims to describe it |
| Web TD `:6` | derived from `:4` (which already carried 840/900), three declared changes only: both images → round-6 digests, allowlist `+source:llm`, binding digest recomputed |
| Images | built from a detached worktree at `69c6ad4b5`, pushed as `acceptance-…-69c6ad4b5` (round-5's tag is immutable and stays addressable) |

The binding digest deserves a note: `ELSPETH_ACCEPTANCE_PLUGIN_POLICY_BINDING_SHA256`
is re-derived by the app at boot from the seven protected policy settings, so
changing the allowlist without recomputing it fails the roll. The build script
**reproduces the existing digest from the unmodified base TD first** and aborts
if that disagrees with terraform's — only then does it recompute. A recomputed
digest from a derivation that was never checked against the authority is just a
different wrong answer.

The ALB check matters for the same reason: the app's boot guard validates the
wall clock against `ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS`,
which is a *claim* about the ALB, not the ALB. A TD asserting 900 against an
ALB still at 300 boots green and silently truncates every request at ~300s —
which would have killed g03 (the round's most expensive compose) and read as
the envelope fix failing.

## Level 2 — live drives

Seven drives, sequential, TD `:6` (composer wall 840 against ALB idle 900).
Sequential by construction: g04's verdict is a *timing* claim, and concurrent
composes on one account also risk the 30/min composer rate limit, which would
surface as failures indistinguishable from product defects.

| Graph | rc | wall | compose | run | mutating calls | review calls | `is_valid` agree | `/state/yaml` |
|---|---|---|---|---|---|---|---|---|
| g08-s1 | 0 | 163s | 107s | `completed` | 2 — repair | 1 / 1 site | ✅ | 200 |
| g08-s2 | 0 | 253s | 201s | `completed` | 4 — repair | 1 / 1 site | ✅ | 200 |
| g08-s3 | 0 | 257s | 206s | `completed` | 4 — repair | 1 / 1 site | ✅ | 200 |
| g04-s1 | 0 | 189s | 164s | `completed` | 3 — repair | 1 / 1 site | ✅ | 200 |
| g04-s2 | 0 | 123s | 100s | `completed` | 1 — prevention | 1 / 1 site | ✅ | 200 |
| g03 | 1 | 193s | 191s | *(not run)* | 1 — prevention | 1 / 1 site | ✅ (both `False`) | 409 |
| g11 | 0 | 492s | 467s | `failed` | 5 — repair | 0 | ✅ | 200 |

### `elspeth-41bcaa882e` / `elspeth-902fc354b2` — **3/3 clean. Unblocked.**

Round 5: 1 completed, 2 validate-FAILED on the `row_union` edge with
`suggestion: null`. Round 6: three `completed` runs. The protocol asks which
path each sample took, and the transcripts answer: s1 repaired once, s2 and s3
repaired four times each (`set_pipeline[failed] → set_pipeline[applied] →
get_pipeline_state → patch_node_options[rejected] → upsert_node[applied]` for
s2). Round 5's g08-s2 made **zero** repair attempts.

**What the live samples do and do not evidence.** They show the composer
repairing and converging. They do **not** show *which* rejection each repair
answered: the transcript API does not expose tool-result rows
(`elspeth-de3638b6ac`), and the `/state` captures are the final, valid states,
so `validation_errors` and `validation_suggestions` are `null` on all three.
g03 in this same round proves rejections come from other seams too. The
suggestion-channel claim therefore rests on L1, not on these transcripts.

**L1 repair probe — the channel verified directly.** PREVENTION means the
unmodified round-5 form no longer reaches the missing-fields arm, so the
repaired channel cannot be observed from it. The probe takes that same authored
form and demands one field no branch can guarantee, so the arm fires
(`r6-evidence/l1-41bcaa882e-repair-probe.txt`):

| | Round 5 (`59cb6f75e`) | Round 6 (`69c6ad4b5`) |
|---|---|---|
| exception | bare `GraphValidationError` | structured `EdgeContractError` |
| producer guarantees | `(none - dynamic schema)` | `['complaint_id','complaint_text','style_tag','summary','summary_model','summary_usage']` |
| `missing_fields` | — | `('no_branch_supplies_this',)` |
| `from_component_type` | — | `row_union` (new in `c408ea870`) |
| suggestion | `None` | non-null and actionable |
| remediation names | `required_input_fields` only | *"its required_input_fields option **or its schema.required_fields**, whichever declares them"* |

The suggestion the web preflight emits:

> The plugin-free row_union … exposes an engine-owned observed schema; it has no
> plugin schema options to patch. Relax the real downstream consumer transform
> … Tool: `patch_node_options(node_id=…, patch={'schema': {…}})`

That is defect #3 answered precisely: it recognises `row_union` has no options
to patch — which is exactly why round 5's advice to add `guaranteed_fields` was
unusable — and redirects to the node that does. All three of the ticket's
defects are verified with controls.

### `elspeth-9d59c33480` — **loop gone; one caveat**

Every one of the six samples that staged a review issued **exactly one**
`request_interpretation_review` call over exactly one site, zero repeats.
Round-5 arm-A g04 shows **four** attempts at the same site. g04's composes
landed at **164s and 100s** — inside the old 270s parity wall, and shorter than
round-5 arm B's 232s, which is the ~40–110s the loop burned, recovered. No
driver `/resolve` rescue was needed to break a deadlock.

**Caveat, recorded as `elspeth-obs-8ad9b34eea`.** The byte-match is avoidable,
not impossible. `_assert_affected_component` still compares when a draft is
supplied — `if llm_draft is not None and draft is not None and draft !=
llm_draft: raise ToolArgumentError` — and in **all** round-6 samples the model
still *sent* `llm_draft` despite the new OMIT guidance. These passes were
therefore carried by the model's re-emission happening to match, **not** by the
server-side staged-draft resolution the fix introduced. Round 5's deterministic
failure mode remains reachable on any compose whose re-emission diverges. A
green round does not distinguish "fixed" from "fixed while the model
complies", and the closure note should say so.

### `elspeth-09c91778f5` — **envelope closed, and then overtaken**

g03 composed in **191s**. Round 5 could not reach its first `set_pipeline`
inside 270s and needed ~490–514s end to end. Two landed fixes compound here:
the raised envelope, and the `type_coerce` composer-hints fix
(`elspeth-697f455a1d` / `a380daec6`) that removed the discovery turns g03 was
spending. g03 now fits inside even the **old** wall.

The ticket's live criterion is met — but note honestly that it is met by a
compose so much faster than predicted that the raise was not what saved it.
The raise remains correct (it was taken as a package decision and g11 needed
467s, well past 270), but g03 is no longer the evidence it was designed to be.

g03 then failed `/validate` on a genuine authoring defect, filed as
`elspeth-85f3cc3022`: the composer declared `price` as `int` on one branch and
`str` on the other and fed both into a coalesce union merge, with **one**
`set_pipeline` call, no `preview_pipeline`, and no self-check before handing
back. This was invisible in round 5 because the graph never got far enough to
be judged.

### `elspeth-155947ca47` — **agrees on 7/7, including where it matters**

Every sample's persisted `is_valid` matches `/validate`. The decisive one is
g03: persisted **`False`**, `/validate` **`False`**, and `/state/yaml` correctly
409. Round 5's preserved g08-s2 had persisted `True` against `/validate`
`False` on an invalid state — the exact inversion. Verified on the condition
the fix governs, not merely on happy paths.

### g11 — llm **source** sampled for the first time in six rounds

With `source:llm` authorised, the composer authored a real
`source: {plugin: llm, prompt_template: …, response_field: llm_response}`,
repaired across five mutating calls, called `preview_pipeline` twice, and
reached a valid state that executed. The **source mechanism works**: it read
1 row in 4.5s.

The run then **failed at the text sink**, and — separately from why — nothing
says why. Filed as `elspeth-9595abb7b0`: the token state reports the sink node
`failed` with `error_message: None`, the operations array reports the *same*
node's `sink_write` as `completed` with `error_message: null`,
`failure_detail` is `null`, and `discard_summary.stages[0].node_id` is `null`.
The artifact is 0 bytes (`content_hash` is the SHA-256 of the empty string).
The root cause of the write failure is **still open** — a CloudWatch look at
the task log for the run window should name it; AWS credentials expired before
that could be done and the evidence was already sufficient to file.

### `elspeth-b19dfe41fb` — L2 discharged without spending a compose

Round 6's g11 authored a *different* shape (no shield on an int field), so the
live leg was not exercised by the drive. It did not need to be: the rejection
is **statically knowable**, so the shape can be put in front of the live
preflight directly. Round 5's own g11 export was imported into a fresh session
on the round-6 stack via `POST /api/sessions/{id}/state/yaml` and validated:

```
is_valid: False
FAILED graph_structure ::
  Transform 'aws_bedrock_prompt_shield' (node 'transform_prompt_shield_auto_1_…')
  scans input fields that must be text, but its upstream 'source_source_…'
  declares them non-string: 'sentence_num' is declared int. …
```

Round 5 passed this same shape and then quarantined 100% of rows. **L2 met,
zero composes.**

Two incidental notes from that import, both showing controls working as
designed: a `csv` source path of `inputs/…` and of `outputs/…` were **both**
rejected with *"Path traversal blocked … resolves outside allowed
directories"* — the web surface does not accept author-controlled filesystem
source paths, so the import had to go through an uploaded blob and
`source_blob_ids`. And the round-5 export carried neither the source path nor
the sink path, which is consistent with `elspeth-b73666ac82`: these exports are
records, not re-runnable artefacts.

## Verdicts

| Ticket | L1 | L2 | Verdict |
|---|---|---|---|
| `elspeth-41bcaa882e` | PREVENTION, control-verified | 3/3 clean, repair exercised | **PASS** — recommend close, and close `elspeth-902fc354b2` with it |
| `elspeth-9d59c33480` | contract changed, control-verified | 1 review call per site, 6/6; g04 164s/100s | **PASS with caveat** — see `elspeth-obs-8ad9b34eea` before closing |
| `elspeth-09c91778f5` | n/a (infrastructure) | g03 composes in 191s; ALB 900 verified on the ALB | **PASS** — criterion met, though not for the predicted reason |
| `elspeth-155947ca47` | divergence reproduced in r5 evidence | 7/7 agree, incl. an invalid state | **PASS** — recommend close |
| `elspeth-b19dfe41fb` | build-time rejection, control-verified | live `/validate` rejects round-5's exact shape | **PASS** — recommend close |

## New defects found this round

| Ticket | P | Summary |
|---|---|---|
| `elspeth-b73666ac82` | 1 | Composer-exported YAML cannot be loaded by the batch/CLI loader — `resolved_prompt_template_hash` is a private profile option |
| `elspeth-ba6a8dff24` | 2 | AWS ECS package default allowlist omits `source:llm`; no cold install can author an LLM source the package ships |
| `elspeth-de3638b6ac` | 2 | Session transcript API omits tool-result rows and redacts correction detail |

### `elspeth-b73666ac82` in brief

`GET /api/sessions/{id}/state/yaml` returned **200** for round-6 g08-s1 and
emitted `resolved_prompt_template_hash` on each profile-selecting llm node.
That key is in `LLM_PROFILE_PRIVATE_FIELDS`, and the batch loader's profile
lowering rejects any private key on such a node. Isolated deterministically
against the live export:

```
as exported                                 -> ValueError: private_profile_option
with resolved_prompt_template_hash removed  -> LOADS CLEAN
```

This bears directly on the product claim in `AGENTS.md` that the two authoring
surfaces *"target one runtime model"*: the Composer's own sanctioned export is
not loadable by the YAML surface. Rounds 1–5 never caught it because the
battery drives the web surface end to end and never re-loads an export. The fix
direction is a genuine product decision (is an exported YAML a re-runnable
artefact, or a record?) and is left to the operator rather than guessed.

## New defects found this round (full list)

| Ticket | P | Summary |
|---|---|---|
| `elspeth-b73666ac82` | 1 | Composer-exported YAML cannot be loaded by the batch/CLI loader — `resolved_prompt_template_hash` is a private profile option |
| `elspeth-ba6a8dff24` | 2 | Package default allowlist omits `source:llm`; no cold install can author an LLM source the package ships |
| `elspeth-de3638b6ac` | 2 | Session transcript API omits tool-result rows and redacts correction detail |
| `elspeth-9595abb7b0` | 2 | Sink write failure is undiagnosable; two surfaces disagree on the same node |
| `elspeth-85f3cc3022` | 2 | g03 authors a type-incompatible coalesce union merge and never self-checks |
| `elspeth-obs-8ad9b34eea` | obs | `9d59c33480`'s byte-match trap survives the fix; round 6 did not exercise the fix's own mechanism |

## What round 7 owes

1. **The g11 sink root cause** — one CloudWatch query on run
   `badfc85f-5657-416e-9624-7cfe5caedcf5`'s window names it.
2. **g01/g02 second stochastic pass** — still carried from round 5; untouched
   here because the operator scoped this round to the priority trio.
3. **Advisor END gate** — still unmeasured; zero `phase=end` events in rounds
   3–5, and round 6 ran no cost/advisor pass at all.
4. **Cost pass** — not run this round. Seven composes at TD `:6`; the
   per-arm cohort query (`make_r5_cost_override.py` pattern) would give a
   900/840-render figure to set against round 5's 0.3077/session parity number.

## Proportionality note

This round spent 7 composes, and all five ticket verdicts closed. Three were settled
*deterministically off-stack, before any compose*, by replaying round 5's own
preserved artefacts against both code pins with the same harness. That is what
made the live legs cheap: they were confirmation, not discovery. Two of the
five new defects (`b73666ac82`, `ba6a8dff24`) were also found off-stack, and
one of them retracts two rounds of prior verdicts.

## Stack state at hand-off

Service on web TD `:6`; ALB idle 900 (verified on the ALB); TLS is the ACM cert
on `elspeth.aws.foundryside.dev` via SNI attach, so no cert clock. TD `:3` =
r5 parity 270, `:4` = r5 arm-B 840/900, `:5` = r5 stock 240, `:6` = round 6.
`source:llm` is authorised on `:6` and in the scenario-A tfvars (which is
gitignored, so it is local operator config, not a tracked change). A full
`terraform apply` remains DIRTY — always plan-gate and target. **Cleanup
deadline 2026-08-09.**
