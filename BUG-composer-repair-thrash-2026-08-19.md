# Bug: the composer burns turns varying payloads instead of changing tool or re-reading schema

- **Reported**: 2026-08-19
- **Environment**: DTA-Dev AWS ECS, `elspeth-web`
- **Session**: `7aa0ab13-8f2e-457c-97a7-da0427c193f2` (`user_id=johnm`)
- **Builds observed**: `07c803703` (task def 21) and `c668d25f9` (task def 25)
- **Severity**: Medium — every occurrence converged eventually. The cost is
  latency, tokens, and operator confidence, not correctness.

All times below are AEST (the deployment stores UTC; converted here for
legibility against the operator's own timeline).

## Summary

Three separate stretches in one session where the composer spent turns it did
not need to. The pattern is consistent: on a validator rejection it varies the
*same* payload rather than re-reading the schema or switching to a
narrower-grained tool. ELSPETH's own drift guard caught the worst instance and
corrected it in one turn — which is good, and also evidence that the condition
is detectable earlier than it currently fires.

Worth stating plainly: the surrounding work was competent. The model caught a
UTF-8 BOM unprompted, batched three inspection calls into a single turn, flagged
"quality of submission" as a vague term needing a review card rather than
silently inventing a rubric, and correctly judged a prompt-injection warning
advisory for an uploaded CSV while still surfacing it as a decision. The defects
below are narrow.

## Stretch A — three failed `set_pipeline` calls (build `07c803703`)

18 Aug, 11:28:00 → 11:30:02. The user asked for an LLM transform rating two case
studies 1–5.

| Time | Event |
|---|---|
| 11:28:00 | `set_pipeline` → `success:false` |
| 11:28:18 | assistant: "I just need to opt out of the top-level `required_input_fields` check with `[]`. **Retrying with the same full topology**" |
| 11:28:18 | `set_pipeline` → `success:false` |
| 11:28:36 | `set_pipeline` → `success:false` |
| 11:28:36 | `[ELSPETH-SYSTEM-HINT]` drift guard fires |
| 11:30:02 | `set_pipeline` → success, `is_valid:true` |

The guard's text names the problem exactly:

> Your last 3 calls to `set_pipeline` all failed while sending different
> arguments. This is drift without convergence: small payload variations are not
> escaping the validator failure. … **When the goal is a one-node linear
> insertion, switch to `splice_transform` instead of varying full-replacement
> payloads.**

Three distinct defects are visible here.

**A1 — wrong tool granularity (root cause).** The requested change was one
transform inserted between an existing source and sink. The model used
full-replacement `set_pipeline`, so every retry resubmitted the entire topology
and every rejection could have originated anywhere in it. `splice_transform`
exists for precisely this shape; the guard names it only *after* three failures.

**A2 — repair by guessing.** The 11:28:18 turn states the intent outright:
opt out of a check and resend the same topology. That is variation without a
hypothesis. Two genuinely different root causes were in play and neither had
been diagnosed yet (see A3 and the `row` binding for `multi_query`).

**A3 — the model failed to match a string it authored itself.** The backend
projects `requirement_id` as `<user_term>:<node_id>`. The model's own user_term
was `quality of submission` (with spaces), but it sent
`quality_of_submission:rate_cases`, underscored. It normalised a value it had
written moments earlier, then could not match it. Its eventual diagnosis at
11:30:02 is correct; nothing in the input required the underscoring.

## Stretch B — two failed `patch_node_options` calls (build `c668d25f9`)

19 Aug, 13:15:32 → 13:16:08. The user asked for a rationale alongside the score.

| Time | Event |
|---|---|
| 13:15:32 | chooses `patch_node_options` for a narrow edit — correct instinct |
| 13:15:32 | fails: `resolved_prompt_template_hash` stale |
| 13:15:48 | assistant: "I'll include it as `null` in the patch to let the backend recompute it" |
| 13:15:48 | fails: the hash is runtime-owned and cannot be set via patch |
| 13:16:08 | switches to `upsert_node` omitting runtime-owned fields → success |

Milder than Stretch A: each retry was driven by a genuinely new error and it
self-corrected without the guard. The avoidable step is nulling a runtime-owned
field — a guess that the schema should have made unnecessary.

## Stretch C — repeated work after a cancelled turn (build `c668d25f9`)

| Time | Event |
|---|---|
| 13:29:14 | assistant: "Now let me inspect the source to get the exact application ID field name." → inspection call succeeds |
| 13:30:03 | `llm_call_audit` `status:cancelled` — turn aborted mid-flight |
| 13:40:08 | user: "continue" |
| 13:40:45 | assistant: "I need to inspect the source to get the exact application ID field name before rebuilding." → **same inspection, repeated** |

Completed tool results from before the cancellation were not carried across the
resume, so the model redid work it had already done.

## Cost telemetry

Individual composer calls in this session recorded 48,613 and 55,832 total
tokens (~US$0.06 each). Useful as a baseline: repair loops on this surface are
expensive per turn, which raises the value of converging in fewer of them.

## Diagnosability defect

The stored audit records redact the validator output. Every `error_code` and
`message` in `chat_messages` reads `<redacted-response-text>`. The three failure
causes in Stretch A were only recoverable from the assistant's own prose
describing what it thought was wrong.

That means post-hoc analysis of composer failures currently depends on the
model's self-report being accurate. This is the same class of gap recorded in
`BUG-guided-passthrough-validation-deadlock.md`, appearing in a different table.

## Suggested fixes

1. **Fire the drift guard earlier.** Two failures of the same tool with the same
   `error_code` is already drift. The guard's advice was correct and immediately
   effective; the cost is that it arrives on the fourth attempt.
2. **Give tool-granularity guidance before the first mutation**, not as
   post-failure advice: single-node insertion → `splice_transform`; narrow
   option edit → `patch_node_options`; topology rebuild → `set_pipeline`.
3. **Mark runtime-owned fields in the schema** (`resolved_prompt_template_hash`
   and peers) so they are visibly not settable, and have the rejection name the
   tool that can achieve the intent.
4. **Fix the `requirement_id` projection asymmetry** — either normalise
   `user_term` identically on both sides, or return the expected projected id in
   the rejection so no guessing is required.
5. **Persist validator messages unredacted** under an operator-visible audit
   scope. Without them this analysis is not reproducible from data.
6. **Preserve completed tool results across a cancelled turn** so a resume does
   not repeat work.
