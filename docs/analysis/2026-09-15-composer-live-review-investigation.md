# Composer live-review investigation

Follow-on implementation: the user subsequently authorized repairs. The change
adds one audited reply-only provider call at terminal review handoff, a trusted
notice when that reply is unavailable, and a versioned
`approved_prompt_artifact_hash` across config, events, providers, calls and
exports. Fully overridden multi-query nodes no longer require a dead fallback.
Single-query system prompts are now shown in review drafts and bound by drift
checks too. Requirement-level review hashes remain separate. An artifact event
locates the effective prompt; complete approval evidence also includes the
composition's requirement events and history.

The reply-only request preserves historical tool records as attributed text so
providers that prohibit tool protocol blocks without advertised tools can accept
it. Uploaded prompt content keeps its blob identity and resolution provenance;
an unresolved blob does not acquire a full-text approval hash through later
substitution. Individual review evidence remains enforced independently. An
unused fallback does not affect the effective prompt artifact.

The frontend uses successful mode probes and readable approval labels, and
displays saved model/profile choices and discard/merge consequences independently
of planner prose. Planner guidance distinguishes adapted prompts from verbatim
text. Formatting remains an explicit authoring choice; audited outputs are not
silently rewritten. These repairs introduced Session/Landscape epochs 57/41, with the
deployment procedure in the session DB reset runbook. No live database reset is
part of this implementation. Integration evidence is tracked under
`elspeth-3983cd84f1` and `.claude/lanes/composer-followons/`.

The subsequently requested [quota repair](2026-09-15-quota-enforcement-repair.md)
advances the same release to Session/Landscape epochs 58/42.

The sections below preserve the initial investigation and its original measured
state, before implementation.

Investigated 2026-09-15 against `release/0.8.1` at
`6343e22077de50dee9979e8f18eba10a2d15a5f1` plus its existing uncommitted
hardening changes. This is an investigation, not an implementation or release
gate. Product code and tracker records were not changed.

## Evidence and limits

Read the hardening handover, original `REVIEW_34ecff0c.md`, captured API responses
and CSV; independently queried the current session and Landscape databases and
the archived pre-hardening session database read-only. Current source, selected
HEAD comparisons, controlled config probes, and focused tests substantiate the
mechanisms below. No new live provider run or injected branch failure was made.

Raw current database results:

```text
assistant rows: (sequence, has_tool_calls, raw_content_length)
(2, True, 193), (8, True, 554), (10, True, 121), (12, False, 0),
(18, True, 409), (24, True, 209), (26, True, 91), (29, False, 2056)

archived handoffs: (sequence, session_prefix, raw_content)
(16, '94f6f00c', ''), (15, '3b6d57d0', ''), (26, '3b6d57d0', '')

run db86b903: completed, rows_processed=5, rows_succeeded=5, rows_failed=0
token outcomes: [('success', 15), ('transient', 5)]
calls joined through node_states for this run:
('http', 'success', NULL, 20)
('llm', 'success', '4abd01205601f7fb42c5bfb61cc65aa3db2d44b8f7d66165c0f60c9760e63cf9', 20)
```

Tool-call JSON presence was controlled against nonempty, empty, null-string and
null inputs. The archived handoff SQL pattern matched a known notice and rejected
ordinary prose (`LIKE controls (1, 0)`). The three prompt-review events for
`ask_two_questions`, `ask_decorator`, and `ask_assistant` independently carry the
same `4abd0120…` runtime hash. These measurements corroborate the reported run
success and missing reply; they do not prove all generated-data sessions fail.

## 1. Missing answer at terminal review handoff — confirmed, highest priority

`service.py:1131` recognizes a clean terminal review-tool suffix.
`service.py:5515` finalizes using the tool turn's prose without another provider
response. `sessions/routes/_helpers.py:1494` avoids duplicating already-persisted
prose by emitting only the trusted notice with empty raw content.
`frontend/src/components/chat/turns.ts:169` excludes tool-call narration from
genuine replies. Together these leave no visible answer. The explanation remains
in stored row 8; row 12 contains only the notice.

The decisive behavior exists in HEAD. The scope is successful terminal review
batches; mixed failures and nonterminal batches take other paths. Archived
examples establish recurrence, not a universal session census.

**Recommended repair:** one bounded provider reply with tools disabled after
review staging and necessary preflight repairs. Keep narration suppression and
deduplication. Do not re-enter the authoring loop. Persist the new reply with its
own provenance, preserve pending cards, and enforce existing deadline,
cancellation and lease authority. An empty/failed reply needs an honest failure
outcome rather than promotion of earlier narration.

Regression coverage must cross compose, route persistence and frontend grouping,
including reload, recompose, empty tool narration, mixed failures and provider
failure. Assert one additional non-authoring call and no duplicated reviews.

Tracker: `elspeth-d581b3da7f` remains `fixing`, claim expired September 2;
`elspeth-e074575b6e` is closed. Their individual requirements should remain intact.

## 2. Multi-query runtime hash names an unused template — confirmed

`plugins/transforms/llm/base.py:211` requires the node template unconditionally;
its validator at line 418 requires the runtime hash to equal that string's hash.
`sessions/pending_interpretation.py:1206` writes that hash to the event/runtime
config. `plugins/transforms/llm/transform.py:1479` forwards it to providers even
when every query overrides the template. Actual query-template evidence is
recorded separately at line 825.

The distinct review-surface anchor includes system prompt and query identities
(`web/interpretation_state.py:2395`). Controlled probes produced:

```text
positive_config_valid=True
template: runtime_anchor_unchanged=True, review_anchor_changed=True
system_prompt: runtime_anchor_unchanged=True, review_anchor_changed=True
query_name: runtime_anchor_unchanged=True, review_anchor_changed=True
missing_node_template: rejected
actual_query_hash_as_runtime_anchor: rejected
```

This is an audit-attribution defect, not demonstrated approval bypass. Shared
content hashes alone are not defective: hashes need not uniquely identify nodes.
The problem is that this one attributes calls to an unused template.

**Recommended repair:** distinguish approved-artifact identity from effective
per-query template identity. Decide compatibility/version semantics before
changing the cross-database field. Cover config, Composer materialization,
session events, provider calls, read/export paths and audit traversal together.
Permit no node fallback only when every query supplies a valid template. Preserve
fallback behavior for mixed queries and single-template nodes.

Tests already pin the dead-template runtime hash, including
`test_prompt_review_card_end_to_end.py:280–320`; this requires intentional contract
repair, not only additional tests. Include system/query/name drift, mixed
fallbacks, shared-content nodes, provider parity and session-to-call traversal.
Persistence changes require PostgreSQL integration checks.

`elspeth-f1a365b714` remains actively claimed through September 16; coordinate
with that existing work rather than conflating the repaired review surface with
the outstanding runtime attribution.

## 3. Remaining findings

| Finding | Verified conclusion | Repair direction |
|---|---|---|
| “Used verbatim” | False: saved templates reword both questions and add output instructions. Review cards expose the actual text. | Planner guidance and behavioral evaluation for honest adaptation disclosure. |
| Failure handling | Planner explicitly supplied discard; source also retains default fills and a repair hint recommending discard (`tools/generation.py:1028`). | Extend `elspeth-0aace271b4` acceptance to explicit choices and user-visible consequences; removing defaults alone cannot fix this incident. |
| Model disclosure | Nodes bind `sonnet`; profile-bound model approval exemption is deliberate (`planner_authoring_aids.py:652`). | Show the resolved binding without inventing an extra operator-policy approval. |
| Output formatting | Bold, case differences and explanatory parentheses are present. Green also differs: `#008000` versus `#00ff00`. No strict no-Markdown/case contract was requested. | Treat as output-contract quality; have the planner author and review explicit requirements when needed. |
| Guided 400 | Persisted freeform state is probed unconditionally; backend intentionally returns 400 and frontend catches it. Blank sessions differ. | Use authoritative mode discovery or a successful typed probe; preserve blank/guided/freeform distinctions. Another catch cannot remove the browser network error. |
| Approval IDs | `ChatPanel.tsx:165` renders raw `userTerm`; its test at line 9522 explicitly expects the internal ID. | Kind-aware human label, retaining node attribution, timestamp and stable audit identity. |

For failure handling, the captured successful run lost no rows. The reported
`require_all` consequence is recorded merge failure and absent CSV output, not
unrecorded disappearance. No fault-injection claim is made here.

## Validation and follow-through

Focused tests completed with explicit exit codes:

```text
reply persistence + terminal review dispatch: 14 passed, exit=0
LLM config + multi-query contract + prompt-review integration: 230 passed, exit=0
```

These passing tests preserve local behaviors that combine into defects; they are
not evidence that the defects are fixed. No full suite or new browser test was
run. Detailed lane reports and terminal logs are under
`.claude/lanes/composer-investigation-20260915/`.

Loomweave's existing index was failed/never analyzed. A refresh was requested
and cancelled before completion once direct investigation finished; no
index-derived structural claim is used in this report. All code findings use
direct source evidence.

Recommended order: repair terminal reply delivery; agree and repair runtime
prompt attribution; address truthful failure/model disclosure; then mode-probe
and approval copy. Formatting remains a lower-priority authoring requirement.
No tickets were created, no fixes committed, and no worktrees removed.
