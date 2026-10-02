# Fix prompt: composer defects from the 2026-09-25 live run

You are fixing six defects that one live Web Composer session exposed on 2026-09-25 (session
`ed3c015b-a2f7-4322-9f39-1716541c5796`, deployed from local `release/0.8.1` with the strict tool-contract work S0 + S1 and
the branch-order fixes, session epoch 67). The maintainer, John, wants these fixed directly on `release/0.8.1`. The lead
agent will review your branch and merge it. Stop at ready-for-merge: do not merge or push.

Read `AGENTS.md` in full before you start, especially Composer invariants, Validate by trust domain, the Gotchas
(worktrees, the two-root `PYTHONPATH`, whole-tree gates) and Commit Hygiene. Read `CONTRIBUTING.md` § Whole-tree gates.
Background: `docs/plans/2026-09-23-composer-strict-tool-contracts.md` (S0 and S1) and
`docs/plans/2026-09-23-composer-strict-tool-contracts-s1.md`.

## Working setup

- Create a worktree from the current `release/0.8.1` tip:
  `git worktree add .claude/worktrees/composer-live-run-fixes -b fix/composer-live-run-defects release/0.8.1`.
  Symlink `.venv` to the main checkout's. Keep lane notes and logs under `.claude/lanes/live-run/` (gitignored).
- Run Python with both roots: `PYTHONPATH=$W/src:$W/elspeth-lints/src $W/.venv/bin/python -m pytest -n 0 -p no:cacheprovider -o "pythonpath=$W/src $W/elspeth-lints/src" ...`.
  Check that `elspeth.__file__` and `elspeth_lints.__file__` resolve inside the worktree. Write every test run to a
  log and read its exit code; never judge a result through a pipe.
- Evidence: read the live session DB only through SQLite's backup API into your lane, and open it
  `?mode=ro`. Never open `data/sessions.db` for writing. Never read `deploy/elspeth-web.env`. Service logs:
  `journalctl -u elspeth-web --since ... -o short-iso`.
  ```python
  src = sqlite3.connect("file:<main-checkout>/data/sessions.db?mode=ro", uri=True)
  dst = sqlite3.connect("<lane>/snap.db"); src.backup(dst)
  ```
  `composition_rejection_events` holds the unredacted planner-facing rejection text.
- Process: for each defect, write a short plan in your lane first. Then write the failing test and watch it fail
  for the stated reason; make the fix; run a real mutation control (revert the fix and watch the test go red).
  Commit **one commit (or a small series) per defect, by pathspec**, after
  `scripts/branch-safety-check.sh --intent commit --base release/0.8.1`, so each fix can land or revert alone.
  End each commit message with the co-author line the harness gives you.
- Gates for each defect: the focused tests, plus every whole-tree gate whose inputs you touch:
  - `tests/unit/test_mock_discipline_baseline.py` (no unspecced `MagicMock`);
  - `tests/unit/elspeth_lints/test_python_file_walker_authority.py` (whole-tree walks go through `tests/helpers/tree_gate.iter_gate_files`);
  - the masquerade gate: `python -m elspeth_lints.rules.masquerade.seed_baseline --check`;
  - the attribute gates;
  - `scripts/check_contracts.py`, with `--write-census` when a `Mapping[str, Any]` signature changes;
  - `scripts/cicd/generate_skill_inventory.py --check` if any teaching text or tool description changes;
  - ruff, `ruff format --check`, and mypy on the touched sources;
  - the trust-tier lint corpus, compared against the base: strip line numbers, sort, then diff. Compare it with the base,
    not with zero, and never stage or edit judge signatures.

  Finish with `scripts/full-suite-gate.sh --execute --detach --stages ruff,mypy,contracts,lints,pytest`. Add
  `testcontainer` if you touch session persistence, SQL or a schema. Attribute every red against a clean
  `git archive` export of the base. If a stored schema has to change, a session epoch bump is pre-approved. Put it
  in its own last commit and sweep every epoch doc pin, as the branch-order fixes did.
- Never: `# noqa` or `# type: ignore`, `--no-verify`, `git stash`, compatibility shims or accepting two forms,
  `/home/<user>` paths in tracked files, the HMAC key, `mcp__elspeth-judge__*`, merge, push or rebase.

## The session, as measured (lead's evidence)

Two composer turns and one run. Times are AEST on 2026-09-25; the DB stores UTC (subtract 10 h).

- 02:37–02:38: turn 1 builds a complaint-triage pipeline. The advisor (`z-ai/glm-5.3`) returns `clean`. The user
  resolves 3 interpretation cards. Run `1a332521` completes: 6 rows, 6 succeeded, in 10 s.
- 02:39:55: turn 2 asks for an LLM category classifier (billing/outage/other) plus a `reference_join` SLA lookup and
  a 4-column CSV. Planner `openrouter/deepseek/deepseek-v4.1-flash`; every call is served by **Together**
  (`provider_served`), dialect `openai_strict`, 32 strict tools. Call latencies are 33 s, 67 s and 85 s (3–8k reasoning
  tokens each), then 8 s. At 02:43:01 `set_pipeline` writes v6. At 02:43:09 the planner says "Pipeline validated
  cleanly" and calls `request_interpretation_review`. A 5th planner call starts at 02:43:09.6 and has still not
  returned when the user **refreshes the browser at 02:45:12**. That call is recorded as `cancelled` after 123,083 ms
  (`CancelledError`). The turn's POST `/messages` never logs a completion.
- 02:45:25–02:45:27: the user resolves both cards (v7, v8). v8 is `is_valid=0` with a **new** error that appears
  for the first time:
  `Edge contract violation between producer node 'transform_attach_sla_c9f2b2179677' (schema 'ReferenceJoinOutput')
  and consumer node 'transform_tidy_columns_a88319f117d4' (schema 'FieldMapperInput'): field 'id' / 'complaint' /
  'category': consumer requires 'str', producer emits 'Any'`.
  `attach_sla` is `reference_join` with `schema.mode: observed`. `tidy_columns` is `field_mapper` with
  `schema.mode: flexible`, fields `id: str`, `complaint: str`, `category: str`, `response_sla_hours: str`, and
  `select_only: true`.
- Validation history: v6 (the `tool_call` writer, lane `authoring_only`) listed only `vague_term` and
  `llm_prompt_template`. v7 listed only the remaining `llm_prompt_template review pending`. v8 showed the edge
  contract.
- 02:45:42: the user asks "I'm blocked by this ... Graph validation failed. What does it mean ...?". Turn 3 then
  calls `preview_pipeline` **10 times**, and every call is rejected (see defect 0). The planner tries
  `patch_node_options(node_id="transform_tidy_columns_a88319f117d4")`, the runtime alias taken from the error text,
  and gets "Node ... not found". It then retries with the authored id `tidy_columns` and succeeds (v9 and v10 are
  valid). It narrates "Let me invoke the preview with no arguments at all — that tool takes none", and the call is
  still rejected.

## Defect 0 (P0, a regression from S1): zero-argument tools always fail when sent strict on the deployed route

**Evidence.**

| Tool | Before S1 (archived session DBs, no strict) | After S1 (live, `strict_sent=True`) |
|---|---|---|
| `preview_pipeline` | 36 of 36 OK | **0 of 9 OK**, all `ARG_ERROR`, `schema_shape`, `wire_conformant=False` |
| `list_blobs` | 19 of 19 OK | 0 of 1 OK |
| `get_pipeline_state` (has properties, control) | 26 of 26 OK | 2 of 2 OK |

The server's audit records `field_count: 1` for every rejected call, so the model (or the endpoint) always sends
exactly one unexpected key. It does this even when the model says it is sending none. The planner-facing text
is only `'tool arguments' must be an object conforming to the declared argument schema, got invalid_schema`
(`composition_rejection_events`).

`composer_loop_tool_definitions(ToolContractDialect.OPENAI_STRICT)` sends `strict: true` on **10 tools whose
parameters have no properties**: `list_blobs`, `list_composer_blobs`, `list_sources`, `get_expression_grammar`,
`get_audit_info`, `preview_pipeline`, `diff_pipeline`, `list_transforms`, `list_sinks`, `list_secret_refs`. Their
schema is `{"type":"object","properties":{},"required":[],"additionalProperties":false}`. The tools the planner uses to
discover plugins and to verify its work are on that list.

**Fix direction.**
- A strict grammar gives a zero-property tool nothing: its only valid call is `{}`. Stop stamping those tools
  `strict: true`. The fix has two parts:
  - Stamp them explicit `strict:false` on non-NONE transports. That is S1's existing rule for tools that are not
    strict, and `tools/strict_profile.py` or the partition in `tools/wire_projection.py` must record why.
  - Change the strict partition from 32/10 to 22/20. Every pinned count moves with it: the loop and planner
    lists, `strict_capable_tool_count`, the `/api/system/status` `strict_tools` count, the boot-probe error text, and
    the plan documents.
- Keep NONE-route bytes identical to the base, and prove it with the S1 `tool_bytes` technique plus a mutation control.
- Also establish **what the stray key is**, so the fix rests on evidence rather than on a guess. The key is never
  persisted, by design. Measure it offline: point LiteLLM at a loopback recorder, or read the provider's streamed
  delta in a test double. If it can't be recovered offline, record that plainly and treat the key as unknown. Do
  not start persisting model-authored key names.
- If you conclude that the rule should be broader (for example, any all-optional object), state the evidence. Do
  not widen the rule without it.
- Word the rule narrowly: "a tool whose parameters declare no properties is not sent strict". It is about the
  empty-schema shape on endpoints that do not enforce strict. It must not be readable as a general judgement against
  strict tools, because the planned S2 flips (`docs/plans/2026-09-25-composer-strict-contracts-s2-plan.md`) make the
  `patch_*` tools and others strict later.
- The S2 plan and the master plan cite "32 strict tools" in several places. Re-anchor every one of them to the
  partition that actually lands.

## Defect 0b (P1): a root-level `additionalProperties` rejection gives the planner nothing to act on, so it loops

The S1 repair signal (T9: a closed `loc` with `validation_errors` on S-gate rejections) did not reach these
rejections: the payload is bare `{error_class, error_message}`. For an unexpected key at the root of a
**zero-property** tool, the planner must be told, in closed, server-authored text that never echoes the model's key,
that this tool takes no arguments and should be called with `{}`. For a root additional property on other tools,
it should name the allowed properties. Find out why T9's `validation_errors` were absent here: whether the path,
the dialect or the category excludes them, or whether they are dropped before persistence. The lead has confirmed
one mechanism already, in `protocol.py:1074-1091`, `_canonical_tool_argument_expectation`. When the jsonschema
producer's message has a parenthesised summary that is not a type fault, the function returns only
`_TOOL_ARGUMENT_SCHEMA_EXPECTATIONS[argument]`. If the argument is not in that mapping (`protocol.py:879`), it returns
the generic "an object conforming to the declared argument schema". Either way the "Additional properties are not
allowed" detail is thrown away. Dropping the model's key is deliberate, so keep it dropped. What is missing is closed
guidance to put in its place. Pin the fix with a
compose-loop test that drives a `preview_pipeline` call carrying `{"x": 1}`.

Also check whether a **repeat guard** exists. The planner sent the identical failing call 10 times in one turn. If
nothing stops identical consecutive argument errors from spending the turn budget, propose one in your plan. It
must be a rejection or budget rule, never server authoring.

## Defect 1 (P1): pending interpretation reviews hide later validation from the planner

`web/execution/validation.py` `validate_pipeline` (about `:415`–`:700`) applies phases in sequence through
`_apply_phase`. On failure a phase ends the ledger, and `_skipped_checks` (`:276`) marks every later check as skipped.
`_review_interpretations_or_drift_failure` (`:231`) runs **before** YAML materialisation, the runtime settings load,
plugin instantiation and graph construction, and edge-contract errors come out of graph construction. So while any
interpretation review is pending (and `allow_pending_interpretation_placeholders` is false), the planner-visible
result lists only the pending reviews. The pipeline's type contracts are never checked.

The composer already has a tolerant mode. `ComposerServiceImpl._runtime_preflight(...,
allow_pending_interpretation_placeholders=True)` (`composer/service.py` about `:2928`, with the worker at about
`:3331`–`:3345`) is used when `interpretation_tolerant=True` (`service.py:3435`, `:5095`; `tool_batch.py:2346`).
`sessions/models.py:815` documents the `validation_lane` split: `authoring_only` is Stage-1 narrowed by pending
review sites, with no instantiation and no preflight, and `strict` is the turn end.

Establish why the planner's validation at v6 and in turn 2 never ran the graph and edge-contract phases with
placeholders. Then make sure a planner turn **cannot report a clean pipeline**, and cannot hand interpretation cards to the
user, while the pipeline has graph or contract errors that the placeholders do not affect. Validation in the
intended way is fine: reviews are about prompt wording and vague terms, not column types.

Write a characterisation test first. With this session's shape (an LLM node with a pending `vague_term` and
`llm_prompt_template`, a `reference_join` in `observed` mode feeding a typed `field_mapper`), the planner's validation
must report the edge contract while the reviews are still pending.

## Defect 2 (P1): an error surfaced by resolving a review goes to the user, not the planner

When the user resolves the last card (`POST /api/sessions/{id}/interpretations/{event}/resolve`, provenance
`interpretation_resolve`), newly unmasked graph errors appear as a user-facing blocker ("Graph validation failed").
John's direction: the composer should have fixed this on its own. After defect 1, this should be rare. Still,
design the path for a resolve that surfaces a planner-fixable error: the planner gets a repair turn, or the user
is offered one explicit action that starts that turn.

Composer invariants apply. The planner does the repair; the server must not author the fix. Any new automatic
provider call on the resolve transition needs a **per-transition provider-call analysis** in your plan, as the
AGENTS.md standing review trigger requires. Prefer the smallest correct design and say which you chose and why.
If the only acceptable designs need a product decision, stop and put the options in your report.

## Defect 3 (P2): a browser refresh cancels the in-flight composer turn

The refresh at 02:45:12 disconnected `POST /messages`, and the in-flight planner call was cancelled
(`CancelledError`, 123 s). The turn's final step was lost. The composer has durable request and progress machinery:
`composer_inflight_requests`, `composer_progress_snapshots`, `web/composer/progress.py` and
`web/coordination/composer_progress_authority.py`, with the route at `web/sessions/routes/messages.py:113` and
`:1181`. Find where client disconnect propagates into the compose loop. Make a turn survive its HTTP request: finish
it, persist the result, and have the reloaded page collect it through `/messages` and `/composer-progress`. Keep an
explicit user cancel working. Test it with a disconnect in the middle of a provider call.

## Defect 4 (P3): long planner calls look hung

During a 60–120 s reasoning call, the page shows nothing new, and the user refreshed, which triggered defect 3.
Make `/composer-progress` report that a planner call is in progress and how long it has been running. Check what the
frontend renders (`frontend/`), and make any frontend change small. If the frontend changes, rebuild it; say so in
the report.

## Defect 5 (P2): edge-contract errors name runtime aliases the planner cannot address

The error names `transform_tidy_columns_a88319f117d4` and `transform_attach_sla_c9f2b2179677`, but the planner's
tools address nodes by their authored ids (`tidy_columns`, `attach_sla`). The planner's first repair attempt failed
with "Node ... not found". In `validation.py`, `_format_edge_contract_failure` / `_format_edge_contract_message`
promise to "ground both ends by node ID" and to offer concrete patch-tool repairs. Make sure the planner-facing
message and suggestion use the **authored** ids that `patch_node_options` accepts (keep the runtime id only as
secondary context, if at all). Pin this with this session's shape.

## Order, scope and report

- Order: defect 0 first (it can land alone, and it is what is hurting live sessions now), then 0b, 5, 1, 2, 3, 4.
  Each goes in its own commit or commits.
- If a defect turns out bigger than one clean change (defect 3 might), finish the others. Stop on that one with a
  written plan and say so plainly. Don't ship half of it.
- Final report, in your lane plus a summary: per defect, the root cause with file:line, the fix, and the failing-first
  and mutation-control logs. Then the whole-tree gates, the full-suite gate `summary.txt` with every red attributed,
  the trust-tier corpus delta, anything you did not fix and why, and any epoch or frontend-rebuild consequence
  for deploy.

Operational note (not your task): until defect 0 lands, the deployed service can be set back to today's
pre-S1 tool bytes with `ELSPETH_WEB__COMPOSER_STRICT_TOOLS=off` and a restart.
