# Triage: the two 2026-08-19 composer bug reports

- **Triaged**: 2026-08-19, against `aa258b046` (working tree), with every claim
  re-checked against the deployed build `c668d25f9` where the reports observed it.
- **Method**: code path read end to end. Neither report's author read the code
  (report #2 says so explicitly). No live DB access to DTA-Dev; where that
  mattered, a stronger code-level discriminator was used instead and is named.

## Verdict summary

| Report | Claim | Verdict |
|---|---|---|
| #2 | Orphan forks are listed to the user | **FALSE** — two independent guards, both in the deployed build |
| #2 | "Operation failed yet committed a session" is incoherent | **BY DESIGN** — documented archived-evidence retention |
| #2 | Fork is not atomic | **FALSE** — settlement is one `_engine.begin()` transaction |
| #2 | Retry produced a second orphan | **TRUE**, but both are hidden; cost is DB residue |
| #2 | `integrity_error` gives nothing to act on | **TRUE** |
| #2 | incident root cause | **FOUND & REPRODUCED** — custody rewriter/verifier asymmetry |
| #1 | Stretch A repair thrash | Pre-dates the 08-18/19 composer work; untested since |
| #1 | Stretch B runtime-owned field guess | **TRUE** — the one clean post-evening defect |
| #1 | Stretch C work lost across a cancelled turn | **TRUE**, distinct mechanism |

## Report #2 — the headline is false

`SessionServiceImpl.list_sessions` (`sessions/service.py:5731`) filters a fork
row on **two independent conditions**:

1. `archived_at IS NULL` when `include_archived=False` (the default, and what
   `GET /api/sessions` passes). `fork_session` creates the child with
   `archived_at=now` (`service.py:13002`) and only clears it at settlement
   (`service.py:13330`).
2. Independently of archived state, a row with non-NULL `forked_from_session_id`
   is listed **only** if a `guided_operations` row exists with
   `kind='session_fork'`, `status='completed'`, `result_session_id = sessions.id`.

The report's own evidence table records both operations as `status=failed`.
Guard 2 therefore excludes both children regardless of `archived_at` — the live
row value is not needed to settle this.

Guard 2 is *precisely* the report's own suggested fix #2. It landed in
`af9488106` (2026-07-19), a month before the incident, and
`git merge-base --is-ancestor af9488106 c668d25f9` confirms it is present in the
build that was running.

`list_sessions` is the sole user-facing listing path (verified: no other
`sessions_table` listing select in `src/elspeth/web/`; every other reference is a
single-row lookup by id). The web client fetches `GET /api/sessions`.

**Conclusion**: the reporter read the database directly. The three-column
`id / title / created_at` layout in the report's "session list" block is SQL
output, not the API's JSON. Severity drops from High.

## Report #2 — retention and atomicity are design, not defect

`fork_session`'s docstring is explicit: staging "atomically creates and binds one
archived child with a frozen blob plan", and "a failed operation retains the
archived child and plan as integrity evidence while compensating only authorized
partial blobs." The route repeats it at `routes/sessions.py:748`: "The failed
child is retained as archived audit evidence. Only its copied blobs are
compensatable; deleting the session would also destroy the frozen plan envelope."

`blobs = 0` on both forks is that compensation working — `cleanup_blobs_for_fork`
deleted the copied blobs on the failure path.

`composition_states = 1` and 72-of-86 messages are the documented fork scope
(state at the fork point; messages *before* the fork message, plus a synthetic
system message and the edited user message), not an interrupted copy.

Atomicity: `settle_guided_fork_operation` runs wholly inside
`_session_pair_locked_begin` → `self._engine.begin()`, which rolls back on any
exception. The archived→active CAS at `service.py:13330` cannot survive a raise
later in that block. Activation and failure cannot both commit.

## Report #2 — what is actually wrong

**Nobody knows what raised the `AuditIntegrityError`.** That is the bug, and the
diagnosability gap is why.

In the failure branch (`routes/sessions.py:713-760`) the route attaches
`RecoveryFailed[...]` notes to `primary_exc`, then calls
`raise_guided_operation_failure(failed)` — which raises a fresh HTTP failure
built from the *settled operation record*. `primary_exc` is chained as
`__context__`, but `app.py:1486` documents a deliberate policy: handler-level
`__cause__` chains log `exc_class` only, **never `exc_info`**, because exception
text may carry DB URLs, bound SQL parameters, or stored secret names. So the
notes are constructed and never serialized anywhere. `routes/sessions.py` has no
logger at all.

That policy is correct and should not be lifted — it is the same Tier-3 boundary
that report #1's fix #5 proposes to remove. **Report #1 fix #5 and report #2
fix #4 are the same redaction control, and lifting it is the developer's call,
not a bug fix.**

The honest, safe fix is narrower: the `RecoveryFailed[...]` notes are
**operator-authored structured strings**, not raw exception text. They can be
emitted under the existing `request_id` correlation discipline without touching
the `exc_info` policy. Likewise the settlement's own raise sites already carry
distinguishing static messages ("...parent is missing", "...failed staged custody
validation", "...lost archived-to-active compare-and-swap") — persisting *which*
one fired costs no Tier-3 disclosure.

**Duplicate orphan**: idempotency is keyed on the client-supplied `operation_id`
(`routes/guided_operations.py:288`), not on
`(parent_session_id, originating_message_id)`. The report shows two different
operation ids against one originating message, so the retry legitimately reserved
a new operation and staged a second archived child. Real, but both children are
hidden; the cost is DB residue, not user confusion.

**Next step, cheap and local**: the local `data/sessions.db` has 89 sessions and
**zero forks** — this path is unexercised locally. Fork a local session that has
a blob and multiple composition-state versions and see whether `integrity_error`
reproduces. Live candidates from the settlement block: `edited_message_id`
(`staged.messages[-1].id`), `expected_current_state_id`, the staged-custody
validation at `service.py:13078`, and the
`forked_from_message_id != operation["originating_message_id"]` comparison —
worth checking whether those two are stored in the same form (str vs UUID).

## Report #1 — the composer work did not cause this

The fork path was **not functionally touched** between the two observed builds.
`git log 07c803703..c668d25f9` over `routes/sessions.py`, `sessions/service.py`
and `composer/tools/blobs.py` returns exactly two commits, and both are unrelated:

- `721f0e2ea` — a docstring-only edit to `guided_full_pipeline_decline`.
- `cf0ecd820` — a fix to `_affected_component_for_inline_field_path`, an inline
  field-path helper, not the fork path.

The fork mechanism dates to `af9488106` (2026-07-19).

**Stretch A** was observed on `07c803703` (08-17 21:18), which pre-dates the
entire 08-18/19 composer block. Several commits in that block target A's exact
class — `c7f55d921` (terminal schema discloses the naming rules it enforces,
which is A3), `a1e530504` and `bb1e9e4ac` (rejections carry facts / report every
defective component), `592fb5c2c` (briefs teach batching). **No observation
exists either way**: A is untested against those fixes, not fixed by them.

**Stretch C** is *not* a regression of `5ab2975b0` ("the planner stops re-buying
what the session already holds"). That commit addresses redundant calls for
information already in context. C is a different mechanism: intra-turn tool
results are not durable until the turn settles. On cancellation
(`pipeline_planner.py:3829`) the `llm_call` audit record is persisted and the
exception re-raised; the in-memory conversation turn is discarded. Making partial
turns durable has its own audit implications and is a design decision.

**Stretch B** is the one clean, clearly actionable post-evening defect: mark
`resolved_prompt_template_hash` and peers as runtime-owned/unsettable in the
advertised schema (report #1 fix #3), so the model does not have to discover it
by guessing `null`.

## Recommended order

1. Reproduce the fork `integrity_error` locally (blob + multi-version state).
2. Persist *which* settlement invariant fired — static message, no `exc_info`.
3. Report #1 fix #3 — runtime-owned fields marked unsettable in the schema.
4. Delete the two ECS orphans if desired; they are hidden, so this is hygiene.
5. Developer's call, not agent work: whether to relax the Tier-3 `exc_info`
   redaction (report #1 fix #5 / report #2 fix #4), and whether fork retry should
   be idempotent on `(parent, originating_message)`.

---

# Addendum: live reproduction, 2026-08-19

Run against `aa258b046` on the local box: frontend rebuilt (`npm run build`),
`elspeth-web.service` restarted, authenticated as `dta_xxxx` over
`--unix-socket /run/elspeth/uvicorn.sock` (no edge, no proxy).

## The fork `integrity_error` reproduces — first attempt

`POST /api/sessions/da976b3c-.../fork` → **HTTP 500 `integrity_error`**.

## The diagnosability defect is confirmed empirically, not just by reading

The complete journal record for that failure:

```
{"status_code": 500, "request_id": "31882ced-...", "event": "http_error_envelope", "level": "warning"}
INFO: "POST /api/sessions/da976b3c-.../fork HTTP/1.1" 500 Internal Server Error
```

That is everything — no exception class, no message, no raise site. Recovering
the cause required patching `AuditIntegrityError.__init__` in a **separate probe
process** to capture a construction-time stack. No operator would do that from an
incident report, which is exactly why the original report could only infer.

## A SECOND, previously unknown fork defect — filed as elspeth-8b7999d1b0

Recovered raise site:

```
service.py:1337 in _child_user_message
  "fork guided correction_messages.message_id references a message outside copied slice"
  <- _strip_guided_profile_in_meta (service.py:1364)
  <- fork_session._sync (service.py:12924)
```

**Mechanism, confirmed.** A fork copies the messages strictly *before* the fork
message; the fork message itself is excluded, because it is replaced by
`new_message_content`. But `_strip_guided_profile_in_meta` remaps the copied
guided state's lineage references (`root_intent_message_id`,
`deferred_intents.originating_message_id`, `correction_messages.message_id`) onto
child ids and requires every reference to resolve inside that slice. When the
guided state records **the fork message itself** as a correction, the reference
can never be remapped and the fork is structurally impossible.

Direct confirmation on `db5d2e55` — its entire correction list *is* the fork
message:

```
correction_messages = [{"message_id": "e4a7cf8f-...", "content_hash": "9735c481..."}]
```

**The failure is fork-point dependent.** Same session, all three user messages:

| fork point | position | result |
|---|---|---|
| `92456911` | 1 of 51 | HTTP 201 success |
| `4bc75c79` | 13 of 51 | HTTP 201 success |
| `e4a7cf8f` | 31 of 51 | HTTP 500 `integrity_error` @ `service.py:1337` |

A separate sweep of 8 sessions forked from their last user message gave 6
successes (including two carrying 2 blobs each — so blob copy, custody rewrite
and settlement all work) and 2 failures, both at `service.py:1337`, both tutorial
sessions whose correction list named that same last message.

The user-facing shape is bad: the failing fork point is the *most natural* one —
the last thing you said. In the tutorial that message is recorded as a
correction, so "fork from my last message" reliably fails.

**Scope correction.** My first pass wrote "any session with a correction is
unforkable". That is wrong: 19 local sessions carry `correction_messages`, and
forks from non-correction points in them succeed. The discriminator is whether
the *fork point itself* is a referenced message.

## This is NOT the incident's raise site — the incident's is below

The `service.py:1337` raise sits **before** the child insert, so staging rolls
back and no child row is created. The DTA-Dev incident's children *did* exist, so
its failure came later. Reproducing that needed a session the local corpus did
not contain, so one was built.

# ROOT CAUSE — the incident, reproduced (elspeth-f478b01787, P0)

Built on the dev instance as `dta_xxxx` with real provider calls
(`openrouter/anthropic/claude-sonnet-5`): uploaded `cases.csv`, then 4 composer
turns -> 70 messages, 12 composition states, 1 blob
(session `f1d91835-6387-4b70-a7f0-c9594577b491`). Forked from each of its 4 user
messages:

| fork point | position | result | child left behind |
|---|---|---|---|
| `fe4cfd1c` | 0 of 70 | HTTP 201 success | active, listed |
| `33f3ea60` | 21 of 70 | HTTP 500 `integrity_error` | 24 msgs / 1 state / 0 blobs |
| `b6d621f7` | 37 of 70 | HTTP 500 `integrity_error` | 40 msgs / 1 state / 0 blobs |
| `7d9d568c` | 55 of 70 | HTTP 500 `integrity_error` | 58 msgs / 1 state / 0 blobs |

Raise site: `service.py:1592` in `_verify_fork_settlement_blob_custody` —
`"Guided fork settlement state retains parent blob custody"`. Settlement phase,
so staging committed first — which is why archived orphans with a full-ish
transcript and exactly one state survive. **That is the incident's signature**
(its children: 72 messages, 1 state, 0 blobs). It also settles the report's open
question: 72 of 86 is the fork *slice*, not an interrupted copy.

## The defect: the custody rewriter and verifier disagree

The **verifier** is whole-payload — it builds `forbidden` from every parent blob
id *and* `storage_path`, then walks the entire state including `composer_meta`.

The **rewriter** (`routes/sessions.py:365`) is field-targeted: source options,
output options, inline content refs, and two guided sub-objects. Nothing anywhere
rewrites `composer_meta.implicit_decisions` —
`grep -rn "implicit_decisions" src/elspeth/web/sessions/routes/sessions.py`
returns zero hits.

The parent blob id appeared in four places in a failing child's state:

| site | rewritten? |
|---|---|
| `sources.data.source.options.path` | yes |
| `sources.data.source.options.blob_ref` | yes |
| `composer_meta...implicit_decisions.entries[0].value` | **no** |
| `composer_meta...implicit_decisions.entries[2].value` (`blob:<id>` form) | **no** |

**Impact:** any session where an uploaded blob was recorded as an implicit
decision is unforkable from every point after that decision. Uploading a file and
adopting it as the source is the ordinary path, so fork is effectively broken for
any session with an attachment.

**Recommended fix:** make the rewriter and verifier share one traversal. Patching
`implicit_decisions` alone closes this instance, but a field-targeted rewriter
paired with a whole-payload verifier will drift again.

## The list-guard claim is confirmed empirically

After the sweep:

- 7 successful forks → operation `completed`, `archived_at` NULL, **all 7 listed** by `GET /api/sessions`.
- 3 failed forks → **zero** child rows, nothing listed.

The read path behaves exactly as the triage predicted.

## Engine and web surface both verified healthy

- `elspeth run --settings examples/boolean_routing/settings.yaml --execute` →
  exit 0, 10 rows, 5 approved / 5 rejected, output CSVs verified by content
  (not just by exit code).
- SPA root HTTP 200; the served `/assets/index-EEFFhIpU.js` is byte-identical in
  size to the freshly built `dist` asset, so the restarted backend is serving the
  new frontend.

## Test residue left on the local box

Probing created **13 fork sessions** and **1 built session**
(`f1d91835-6387-4b70-a7f0-c9594577b491`) under `dta_xxxx` in `data/sessions.db`:

- 10 forks active and user-visible (successful forks);
- 3 archived orphans from the settlement failures (24 / 40 / 58 messages, 1 state, 0 blobs each).

Blob compensation verified: the orphans hold **0** blob rows while the successful
fork of the same parent holds **1** — the copy landed and cleanup removed it, so
the verifier reached the custody check rather than the earlier
"child blob ids do not match the frozen plan" check.

All left in place deliberately rather than deleted unasked — they are the evidence
for the list-guard claim.

---

# NEW DEFECT — the composer no longer converges (elspeth-92fa1fe86e, P0)

Found by re-running the repair-thrash report's own Stretch A scenario on the
current build. In **neither** bug report.

Session `959e7c63-5bbc-4686-a777-e6a009605e5b`, `dta_xxxx`, freeform `/messages`,
real provider calls. Uploaded a 2-row CSV, then: *"set it up as the source, then
add an LLM transform that rates each of the two case studies 1-5 for quality of
submission, and write the results to a JSON file."*

**HTTP 422 after 338 seconds. No pipeline delivered.**

```
reason: convergence_discovery_budget
budget_exhausted: discovery
turns_used: 17
```

That is materially worse than the report describes. Its premise was "every
occurrence converged eventually. The cost is latency, tokens, and operator
confidence, not correctness." Here it does not converge at all.

## The thrash — every call SUCCEEDS

| # | call | charged to |
|---|---|---|
| 1-3 | `list_blobs`, `inspect_source`, `get_plugin_schema(llm)` | discovery |
| 4 | `set_pipeline` | composition |
| 5 | `get_plugin_schema(field_mapper)` | discovery |
| 6 | `patch_node_options(rate_quality)` | composition |
| 7-10 | `patch_node_options(tidy)` x4 | composition |
| 11-14 | `get_pipeline_state` x3, `preview_pipeline` | **discovery** |
| 15 | `patch_node_options(tidy)` — fifth time | composition |
| 16-17 | `get_pipeline_state` x2 | **discovery — exhausted** |

`tidy` patched five times, state re-read five times, every result
`success: true`. This is re-verification after success, not repair after
rejection.

Ten discovery-only turns against `MAX_DISCOVERY_TURNS=10`: the composer dies on
the **discovery** budget while doing **mutation** work.

## The mechanism — an unreconciled pair

`_VALIDATION_ERROR_PATTERNS` in `tools/generation.py` serves the model:

```
r"Invalid options for transform '(.+)':"
  -> "Use get_pipeline_state to see the node's current options,
      then use patch_node_options to fix."
```

`generation.py:1194` confirms this is model-visible in its own words: *"the model
only sees `"Use get_pipeline_state ... patch_source_options"`"*. Two further
drivers: `state_claim_grounding.py:656` and `generation.py:731`.

Meanwhile `cf0ecd820` added `applied_component` to mutation results specifically
to *"eliminate the `get_pipeline_state` round-trip the LLM would otherwise
burn."*

Both are correct in isolation. They were never reconciled, because **the guidance
is keyed on validation errors and the echo is keyed on mutation success** —
independent conditions whose overlap is the ordinary case while a pipeline is
still being assembled. Verified: every tool result in the failing run carried
`success: true` *and* validation errors at once, and results #6-#10 carried
`applied_component` *and* the errors together.

| situation | echo | guidance | verdict |
|---|---|---|---|
| success, no validation errors | present | none | fine |
| **success + validation errors** | present | "re-read" | **redundant — the defect, and the common case** |
| failed mutation | absent by design | "re-read" | legitimately required |
| `set_pipeline` full replacement | never populated | may fire | a real remaining hole |

**Do not simply delete the guidance** — the last two rows need it. Make the fix
text conditional on whether the same payload already carries
`applied_component`. With a split budget this stops being mere waste: those
reads are pure-discovery turns and there are only ten, so a redundant
instruction becomes a hard 422.

Ruled out along the way: the echo is *not* hidden from the model. Redaction runs
on the persist/audit path (`_persist_turn_audit`), so the
`<redacted-response-map>` in `chat_messages` is the stored form only.

## What is NOT established

- **Whether this is a regression.** The operator reports the scenario worked on
  an earlier build. Settling it needs an A/B against `07c803703`; by operator
  decision that is **not being pursued**. It stands as a new bug on its own merits.
- **The chain-vs-fork observation.** This run produced a LINEAR chain
  (`rate_quality` -> `tidy`, `fork_to: None` on every node). No fork topology was
  chosen, so that half is unreproduced.

## Terminology warning

`session_fork` (session branching, UI Edit button, no composer tool can invoke it)
and `fork_to` (pipeline topology the planner chooses) are unrelated subsystems
that share a word. An observation about one cannot explain a failure in the other.
