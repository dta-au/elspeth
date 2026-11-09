# Composer context budget: investigation and build brief

This is a self-contained prompt for an agent session. Paste it as the opening
instruction. It assumes the composer performance remediation branch
(`docs/development/composer-performance-remediation.md`) has landed on main,
so the per-component request byte split from fix 4A is being recorded.

---

## Prompt

You are working in `dta-au/elspeth` on a fresh branch from main. Read
`AGENTS.md` and `CONTRIBUTING.md § Whole-tree gates` before writing code. Use
the canonical scripts (`scripts/branch-safety-check.sh`,
`scripts/full-suite-gate.sh`) rather than raw git or pytest chains, run every
gate as `gate > log 2>&1; echo exit=$?` and never pipe a gate through `tail`.
Do not use `git stash`. Commit and push each landed step on your branch.

### The problem

A real Web Composer user regularly hits a context limit in long sessions. We
do not yet know which limit. There are three candidates and they have
different owners:

1. **Server-side request byte ceiling.** `pipeline_planner.py` measures the
   canonical JSON of `{"messages", "tools"}` before every provider call and
   raises `PipelinePlannerError(code="REQUEST_BYTES_EXHAUSTED")` when it
   exceeds `composer_planner_max_request_bytes` (`web/config.py`, default
   2 MiB). This refusal happens before any provider call.
2. **Provider context window.** The provider rejects the request; the
   composer records the reason as `context_length_exceeded`
   (`composer/provider_errors.py`).
3. **Planner turn or repair budget.** `ComposerConvergenceError` with
   `budget_exhausted`, or `REPAIR_EXHAUSTED`. This is not a context-size
   limit, but a user describes all three the same way, so rule it out
   explicitly.

The fix differs by case. Case 1 is a server-chosen number and a context
policy; case 2 is a model choice plus a context policy; case 3 is a planner
brief defect and out of scope here.

### What is already measured (ground truth, 2026-11-09)

Second planner call of a tutorial compose run, from the recorded
`request_byte_split` in the `tool_calls` envelope of `chat_messages`:

| Component | Bytes |
|---|---|
| Rendered skill (system message) | 136,047 |
| Catalog reference block (user 1: authoring aids + schema evidence) | 41,705 |
| Tool definitions | 38,149 |
| Provider request (user 2: intent, state, conversation context) | 3,226 |
| Within-run continuation (8 messages) | 55,379 |
| Total | 274,506 |

Provider usage for that call: 64,374 prompt tokens, 49,664 served from the
prefix cache. The first call of the run: 49,776 prompt tokens, none cached.
So in a two-turn session the fixed prefix is about 79% of bytes and the
conversation history is about 1%. A long session is different: the
conversation projection has no bound and the escape hatch sends the whole
session transcript. Those are the growth paths to confirm, not assume.

### Phase 1: identify the limit from the affected user's records

Do this before any design. Every claim below must come with the query and its
raw output. The relevant persistence:

- `chat_messages.tool_calls` (sessions store) carries the composer LLM call
  envelopes: `request_byte_split`, `prompt_tokens`, `cached_prompt_tokens`,
  `finish_reason`, `error_class`, `error_message`, `planner_call_ordinal`,
  `messages_hash`. The public field list is `_LLM_CALL_PUBLIC_AUDIT_FIELDS`
  in `composer/audit.py`.
- `token_usage_ledger` (sessions store) has one row per provider call with
  `source='composer'`, `session_id`, `prompt_tokens`, `cached_prompt_tokens`.
- `composer_async_operations.failure_code` and `result_json` hold the
  operation-level disposition; `composition_rejection_events` and
  `proposal_events` hold the planner-level ones.

Steps:

1. Get the affected sessions from the maintainer (session ids or the user's
   identity id). For each, pull every composer call in order and tabulate
   `planner_call_ordinal`, `prompt_tokens`, `cached_prompt_tokens`,
   `request_byte_split.total_bytes`, each `message_bytes` entry, `tools_bytes`,
   `error_class`, `error_message`. Write the table to a file under
   `docs/development/evidence/` (confirm with `git check-ignore -v` that the
   path is tracked).
2. Classify the failure. Look for `REQUEST_BYTES_EXHAUSTED` in
   `composer_async_operations.failure_code` and in the operation's result
   JSON; look for `context_length_exceeded` in the call records; look for
   `budget_exhausted` and `REPAIR_EXHAUSTED`. State which one the user hits,
   how often, and at what turn number and byte total.
3. Attribute the growth. From the `message_bytes` series across the session's
   calls, say which message index grows: the user-2 provider request
   (conversation context), the continuation tail, the reference block, or the
   hatch transcript message. Plot nothing; a table of bytes per call per
   component is the deliverable.
4. Check the configured ceiling and model. Record the deployment's
   `composer_planner_max_request_bytes`, the planner model, its context
   window, and the escape hatch model. Compute the bytes-per-token ratio from
   the recorded pairs so the byte ceiling can be related to the window.
5. Read the growth paths in code and confirm or refute each as the cause:
   - `planning_application.py::_freeform_planner_conversation_context`:
     preserves every authenticated prior user request and every non-empty
     assistant message with no bound. `additional_prior_user_requests_omitted`
     exists on `PlannerConversationContext` but nothing ever sets it.
   - `pipeline_planner.py::_engage_escape_hatch`: replaces the message list
     with the request plus the complete same-session visible transcript
     (`transcript.provider_json`).
   - The within-run continuation: tool round trips accumulate in `messages`
     until the run converges or the turn budget ends.
   - Schema evidence and discovery digest ceilings in
     `planner_authoring_aids.py` (96, 48, 28, 8, 8 KiB): confirm they hold
     in the affected sessions.
6. Report Phase 1 to the maintainer before building. Name the limit, the
   growing component, and the turn at which it fails. If the limit is the
   turn or repair budget, stop: that is a planner brief defect and a
   different ticket.

### Phase 2: design, under these constraints

The constraints are not negotiable; they come from the composer invariants
and the audit model.

- **The planner does the job.** Context policy is server-side redaction of
  what is sent, which is permitted. It must never author pipeline structure
  or paraphrase user authority. No template summary of prior requests, no
  server-written recap. An LLM-produced summary is an extra provider call on
  the latency chokepoint and gets per-transition provider-call scrutiny; do
  not add one without the maintainer's ruling.
- **No tutorial-special path.** Whatever bound applies, applies to every
  session.
- **Derivable state, not held state.** Whether the context is in a reduced
  mode must be a pure function of the persisted history and a recorded
  policy (hash the policy parameters into `planner_policy_hash` or a sibling
  field). A per-process flag or a cache deciding what the model sees is
  forbidden; the recorded `messages_hash` must be reproducible from stored
  facts.
- **Every omission is recorded.** If prior messages are dropped, record the
  history indices dropped, not just a count. Populate
  `additional_prior_user_requests_omitted` and add the index list to the
  conversation context dict; it is part of the audit envelope.
- **Order of sacrifice.** Drop assistant prose before user requests;
  assistant messages are model synthesis, user requests are authority. The
  existing docstring is right that a fresh-looking build instruction can
  depend on an earlier restriction, so a dropped user request must be
  visible in the audit record and, if the planner then asks about it, the
  user must be able to restate it.
- **Thresholds in tokens, from provider usage.** `prompt_tokens` is in the
  ledger; bytes-to-tokens shifts with JSON density. The byte ceiling stays
  as the outer guard.
- **The fixed prefix stays byte-identical across calls.** The skill,
  reference block and tool definitions are three quarters of the prompt and
  are served from cache on every call after the first. Any policy that
  alters them per turn defeats the cache and costs more than it saves. If
  the skill size itself must come down, that is a planner brief edit with
  its own `composer_skill_hash` change, reviewed separately.
- **Hysteresis is the stabiliser, not the policy.** If the policy reduces
  context above a high-water mark, it must stay reduced until a low-water
  mark so consecutive turns do not alternate between shapes. Both marks are
  policy parameters and part of the recorded policy hash. Hysteresis adds
  nothing to the within-run continuation (there is no later turn to flip
  back on); apply it only to cross-turn history.
- **Backwards compatibility.** 0.8.5+ rules apply: existing sessions must
  keep working; a session that is already over the new low-water mark enters
  reduced mode on its next turn with the omissions recorded.

Write the design as a short section in this file (under "Design, as built")
and get the maintainer's ruling on: the two marks, the order of sacrifice,
whether the escape hatch's full transcript is also bounded, and whether the
default `composer_planner_max_request_bytes` changes.

### Phase 3: build

Likely shape, to be confirmed by Phase 1:

1. A pure function in `planning_application.py` that takes the full history
   plus the policy and returns the `PlannerConversationContext` with
   omissions recorded. Unit tests over synthetic histories: below the low
   mark nothing is dropped; crossing the high mark drops assistant prose
   first, then oldest user requests; once reduced, a history just under the
   high mark stays reduced; omitted indices are exact; the same history and
   policy always produce the same context (hash equality).
2. Policy parameters on `WebSettings` with strict types and sane defaults,
   surfaced in the settings-to-runtime contract (see the
   `config-contracts-guide` skill; `python -m scripts.check_contracts` is the
   census).
3. Record the policy hash and omitted indices on the call record and in the
   `tool_calls` envelope; extend `_LLM_CALL_PUBLIC_AUDIT_FIELDS` if a new
   field is added, and update every consumer the whole-tree gates pin.
4. If Phase 1 implicates the escape hatch transcript, bound it the same way
   with the same recording, and state the compatibility decision.
5. Frontend: when an operation fails with `REQUEST_BYTES_EXHAUSTED` or
   `context_length_exceeded`, the user must see an actionable message
   naming the cause and the recourse (start a new session, shorten the
   request). Check `frontend/src/types/index.ts` for the existing error
   shapes before adding one.

### Phase 4: verify

- Scoped tests for every touched module, then `tests/unit/web/composer/`
  and `tests/unit/web/sessions/`.
- Whole-tree gates whose scanned inputs could change: the architecture
  suite (`tests/unit/architecture/ -n 3`, about 50 minutes), the reviewed
  writer and full-source pins, the terminal SQL namespace grammar if any
  `SessionServiceImpl` header changed, and `python -m scripts.check_contracts`.
- Keyless lint corpus before and after
  (`ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing
  .venv/bin/elspeth-lints check --rules all --root src/elspeth`); compare with
  line numbers normalised (`sed 's/:[0-9][0-9]*:[0-9][0-9]*:/::/' | sort`)
  and account for every added or removed finding. Do not hand-edit signed
  allowlist entries; stage them for the operator.
- Replay the affected user's history through the new projection function
  offline and show the turn at which reduced mode engages and what is
  omitted. This is the acceptance evidence; include the table.
- A compose smoke run on the tutorial to show the two-turn case is
  unchanged: same call count, prefix cache still hit on the second call.
- Full `pytest tests/` and the `testcontainer` suites before merge, on the
  maintainer's runners.

### Reporting

Write findings to files under `docs/development/` and return paths with a
short summary. Report test results only from exit codes you read. State what
you did not run and why. Flag the maintainer rulings you need before
continuing rather than choosing silently.
