# Explore-and-pin: closing a producer / consumer / teaching seam

Status: method, executed twice. First on elspeth-68721c71d7 (planner
repair-feedback teaching, merged into release/0.8.0 at 51435dbb5, 2026-09-02);
second on elspeth-e405ad7cd2 (freeform tool-result envelope, 2026-09-04).
Written so the next seam gets the same treatment without re-deriving the
process. §17 sizes both runs and §19 carries what the second one cost.

## 1. What the method is for

ELSPETH has several places where one side of the code **ships** a set of keys
or facts, another side **admits or consumes** them, and a third side
**teaches** a reader (an LLM, an operator, a custody walker) what they mean.
Nothing in the language holds the three sides together: a `Mapping[str, Any]`
on the producer, an allowlist on the consumer, and prose on the teaching side
drift independently, and the drift is invisible until a live session hits it.

Explore-and-pin closes one such seam end to end:

1. **Explore** — enumerate every row of the seam from the live source of
   truth, by AST, never by grep.
2. **Adjudicate** — give every row a verdict (teach / fence / fix the
   producer / retire) and have the operator ratify each one.
3. **Pin** — land a whole-tree gate that derives every side from source and
   fails on any future drift, plus a fence fixture that names the deliberate
   exceptions.
4. **Close structurally** — where the gate can only close a hole
   syntactically, move the invariant into a type plus a nominal runtime check
   so the gate no longer has to chase syntax.
5. **Review, measure, land, trial** — adversarial review to zero findings,
   mutation-test the gate, full suite from a file, merge, deploy, and prove the
   behaviour live.

The output is not a cleaner file. The output is a seam that cannot silently
reopen.

## 2. When a seam qualifies

Nominate a seam when all four hold:

- **Three sides.** A producer set, an admitted / consumed set, and a taught or
  documented set exist and are maintained in different files by different
  mechanisms.
- **Loose typing at the join.** At least one side is `Mapping[str, Any]`,
  `dict[str, Any]`, `Any`, or an open JSON column. This is what puts the seam
  in scope for the strany epic (elspeth-1ab3675b24).
- **A trust boundary.** The bytes cross into the LLM, into a custody walker,
  into an export, or into another subsystem that acts on them without being
  able to ask the producer what they meant.
- **A live consequence.** Drift has produced, or plausibly produces, a wrong
  planner turn, a custody refusal, a fail-closed placeholder reaching a
  reader, or a silent loss. If nothing acts on the drift, the seam is a
  typing chore, not a candidate.

Seams closed or nominated so far:

| Seam | Producer | Admitted | Taught | State |
|---|---|---|---|---|
| Planner rejection feedback | `GuidedCandidateBindingRejected` facts, detail TypedDicts, `route_destination_fact_keys` | terminal-rejection table in `pipeline_planner.py` | `_VALIDATION_ERROR_PATTERNS` in `tools/generation.py` | closed, gate `tests/unit/web/composer/test_planner_teaching_gate.py` |
| Freeform tool-result envelope | `ToolResult` fields in `tools/_common.py` | redaction manifest in `redaction.py` (fails closed with an unknown-response placeholder) | composer skill and catalogue prose | closed, gate `tests/unit/web/composer/test_tool_result_envelope_gate.py` (elspeth-e405ad7cd2); live trial outstanding |
| `composer_meta` envelope | every subsystem writing the column | custody walkers and the fork rewriter in `sessions/service.py` | none | nominated (custody variant) |

## 3. Roles

- **Agent (lane).** Runs every phase below in a dedicated worktree. Writes the
  census, the matrix, the gate, the fixes, the review reports, and the
  evidence. Never ratifies a verdict, never signs anything, never holds the
  judge HMAC key.
- **Operator.** Ratifies verdicts per row (phase 3), decides the merge
  window, fires any judge-signature bundle if the change moved allowlist
  state, and owns the live-trial go.
- **Reviewers.** Three independent seats (adversarial, LLM, systems) spawned
  by the lane for the go / no-go (phase 6). The originating reviewer signs off
  every fix to their own finding.

## 4. Phase 0 — nominate and scope

Deliverable: a ticket under the strany epic naming the seam by its three
sides, the trust boundary it crosses, and the live consequence. Link the
producer, consumer, and teaching files.

Orientation is allowed to use grep (counts of `[str, Any]` per file, say) as
long as the ticket labels it orientation. The census in phase 1 replaces it.
Do not write a grep number into a claim.

Search the tracker first (`issue_search`) for prior adjudication of the same
keys or files. A seam that was partly adjudicated in a signed allowlist
rationale carries that ruling forward.

## 5. Phase 1 — explore: the census

Goal: a table with one row per shipped key, derived from source by AST.

**Producer side.** Enumerate from the constructs that actually ship:

- TypedDicts and dataclasses reachable from the producer, crossed with the
  constructor keyword arguments each call site passes. Recurse through nested
  TypedDicts and `list[TypedDict]`; a walker that only reads the top level
  will miss the rows that matter.
- Raise sites of the owned exception that carries facts. Read the literal
  dict keys at each raise, and refuse to count a site whose facts are not a
  literal (a variable, a comprehension, a `**` splat, a `cast`). Those sites
  become findings, not rows.
- Production registries that already exist (a route-fact-key set, a manifest,
  a per-tool key list). Import them; never retype them.

**Consumer and teaching sides.** Enumerate from where the words live: quoted
leaf names in the teaching prose, keys in the allowlist, arms in the custody
walker. Again by AST or by importing the object, not by regex over the file.

**Matrix.** Join the sides. Every row is in one of:

| Row state | Meaning |
|---|---|
| shipped, taught | in sync; the gate will keep it so |
| shipped, untaught | candidate for a verdict |
| taught, unshipped | stale teaching; candidate for retire |
| shipped, not admitted | consumer will drop or fail closed on it; producer or consumer defect |

Record the counts in the ticket as census numbers. On the first seam the
census was 245 shipped rows collapsing to 64 distinct keys, against a commit
message that had claimed 27. Expect the census to disagree with what anyone
remembers.

**Where it runs.** A dedicated worktree with both source roots on
`PYTHONPATH`, and `elspeth.__file__` checked to point into the worktree, per
AGENTS.md. A census that imported the main checkout is confidently wrong.

## 6. Phase 2 — verdicts

Each untaught or stale row gets exactly one verdict:

- **teach** — write the teaching entry. The entry must be true against the
  code, not persuasive; the judge and the reviewers read the tree. Name the
  key, say what value shape it carries, say what the reader should do with it.
- **fence** — the row is shipped and will stay untaught for a stated reason
  (the key is internal, the reader cannot act on it, or it is on its way out).
  A fence is data in a fixture, with its reason, and the gate reads it. Keep
  the fence count small and cap it; a growing fence is the seam reopening
  under another name.
- **fix the producer** — the row should not exist in that shape. Rewrite the
  site to ship a closed label, split an overloaded key (the first seam split
  `edge_id` from `reused_edge_id` with `incident_owners`), or stop shipping it.
- **retire** — taught but never shipped. Delete the prose and the test that
  pinned it.

Before the verdicts go to the operator, the LLM-specialist seat reviews them
against real wire samples (operator ruling 2026-09-02: the LLM seat is a
first-class reviewer in auditing, review, and synthesis, not a final-gate
add-on). Only that seat can say whether a taught sentence lets the model act
correctly from the bytes it actually receives; the adversarial seat proves the
gate holds, which is a different question. Changed verdicts carry the seat's
reason into the walkthrough.

In parallel, the systems-thinker seat runs a shape-propagation sweep (same
ruling): every faulty shape the census surfaced (a duplicated vocabulary with
no cross-check, a success envelope carrying an error key, a drift counter that
is always non-zero, a loose type whose producer is already typed) is searched
for across the whole tree, and the result is a shape ledger — shape, every
other site with that shape, and whether this lane closes it, fixes it now as
a follow-up, or fences it under a named sibling ticket. The ledger rides with
the walkthrough; nothing in it is parked as a TODO.

Two rules that came out of the first run:

- One prose slot per code. When the same rejection code can ship two fact
  shapes, either teach both shapes in the one entry or split the code. Do not
  let the second shape ride the first entry's wording.
- A terminal rejection is a verdict of its own. If no candidate the reader
  can author clears the rejection, the feedback must say so and tell the
  reader to decline in plain text. On the first seam this became the
  terminal-rejection table keyed on `(code, exact fact-key shape)`, so the
  planner stops repairing instead of looping.

## 7. Phase 3 — ratification

Walk the operator through every verdict before the gate is treated as
authoritative. The walkthrough is a table: key, site, verdict, one line of
reason. Group by verdict, lead with the fences and the producer fixes because
those are the decisions; the teach rows are usually confirmations.

Ratification is per row and lands on the ticket as a comment from the
operator, or as the operator's reply recorded by the lane with the date.
The lane does not proceed to merge on its own reading of a silent operator.

## 8. Phase 4 — pin: the gate

The gate is a whole-tree unit test. Design rules, each learned by losing to it
once:

1. **Derive, never enumerate.** Every side of the seam is computed from source
   at test time. A hand-listed set of keys is a second copy of the seam and
   will drift exactly like the first.
2. **Refuse the escapes.** The walker rejects, with a named reason, every
   construct that would let a producer ship a fact the walker cannot read: a
   dict literal or dict comprehension anywhere inside a fact value, a `cast(...)`
   in a fact value, a starred keyword splat into an owned detail constructor,
   a detail constructor called outside a recognised site, an aliased
   constructor (assignment alias or import alias) that would dodge name
   matching.
3. **Attribute by ownership, not by keyword.** A detail site is a call to an
   owned constructor (read the class name from the class object, not from a
   string), a module-level alias of one, or a `replace(...)` carrying a detail
   keyword. Keying on the keyword alone swept unrelated constructors that
   happened to share a parameter name.
4. **Pin the walker with probes.** Module-level probe TypedDicts and probe
   sites exercise every walker branch, including the nested and
   list-of-TypedDict recursion. The first probe set found a dead `[]` branch
   in the recursion; without probes the gate would have passed while reading
   half the tree. Function-local probe TypedDicts do not work under postponed
   annotations; declare them at module level.
5. **Fence is a fixture, and the fence is itself gated.** The gate fails when
   an untaught row appears that is not fenced, and it also fails when a fenced
   row becomes taught or stops shipping, so a stale fence is retired rather
   than forgotten.
6. **Walk the whole owned root.** The walker root is the subsystem
   (`src/elspeth/web` on the first seam), not the files the lane touched.

Name the test by what it certifies, and put the reason each refusal exists in
the docstring or the assertion message. A future lane reading a red gate has to
learn the rule from the failure.

## 9. Phase 5 — close structurally

Every walker refusal in rule 2 above is a syntactic close: it stops one way of
writing an unreadable fact. Wherever possible, replace it with a structural
close so the language holds the invariant and the walker becomes a backstop:

- Give the fact value a closed type. On the first seam that was
  `GuidedFactValue = str | int | bool | None | list[str]`.
- Admit it nominally at runtime in the owned constructor, per ADR-032
  (validate by trust domain): exact-type checks, no `isinstance` on
  subclasses, copy any list, and raise the audit-integrity error naming the
  key and the offending type. A str subclass or a list of dicts is refused.
- Strip the `cast(JsonValue, ...)` calls the loose type had forced. Keep only
  the casts feeding a consumer that genuinely takes `JsonValue`; the first
  seam removed 48 and kept 5, and the mypy run is what says which is which.
- Pin the type with a test that refuses each disallowed shape and admits each
  allowed one, including the copy semantics.

The order matters: land the structural close first, then simplify the walker
to what the type cannot express. Do not carry both indefinitely.

## 10. Phase 6 — review to zero

Spawn three reviewers on the branch tip, each with the ticket, the matrix, and
the gate, and each with a distinct charter:

- **Adversarial (red-team).** Charter is to disprove the gate: find a way to
  ship a fact it does not see, a test that passes for the wrong reason, a fix
  whose test survives its reversion. Runs until a round returns nothing. The
  first seam took five rounds and surfaced one major and five minor findings.
- **LLM reviewer (first-class, present at phases 2, 6, and the close-out).**
  Reads the seam from the model's side against real serialized samples. Is
  each teaching entry actionable by a model that sees only the wire, does the
  repeat / terminal notice actually stop a loop rather than describe one, and
  at close-out, what can the model now do that it could not before, checked
  against the live-trial transcripts.
- **Systems thinker (first-class, present at phases 2, 6, and the close-out).**
  Emerging patterns and gaps in our thinking: which sibling seams the change
  touches, whether every site in the phase-2 shape ledger was closed or fenced
  under a named ticket, what the registry or type introduced becomes the
  authority for, what new shape this lane created that a later lane must hold,
  and what the next seam should be.

Rules for the rounds:

- Every fix to a finding goes back to the reviewer who raised it for a
  per-finding sign-off. Fix rounds have introduced new defects before; the
  originating seat is the one who can tell.
- Reviewer agents cannot always write files. Save their reports yourself into
  a lane-private scratch subdirectory and cite the path on the ticket.
- The go / no-go is three written sign-offs on one named commit, not a
  summary of them.

## 11. Phase 7 — mutation ledger

Mutation-test the gate and the structural close, not the defect that
motivated them:

- For each guard, apply a mutation that should turn the gate red (drop a
  refusal arm, widen the type, remove a fixture row), run the gate, restore.
- Restore by copying the file back from a saved copy. Never restore with a
  git checkout of the path; that reverts to HEAD and erases uncommitted work.
- Verify the mutation string was actually found before counting a result. A
  replacement that matched nothing leaves the code unchanged and reports a
  false "survived". Make replacements independent of line wrapping, because
  the formatter will have rewrapped the text since you last read it.
- Record every mutation, whether it was applied, and the gate's exit code in
  a ledger on the ticket. A mutation that survives is a finding for phase 6.

## 12. Phase 8 — evidence

- Full suite in the worktree, written to a lane-private log, exit code read
  from the process, not from a pipe. Launch under `setsid nohup` with a done
  marker for anything that outlives a tool call.
- Lint gate corpus compared before and after the change with the shape-only
  verify mode. The corpus is compared to the base commit's corpus, never to
  zero; the trust-tier gate is deliberately red.
- `check_contracts` ratchet for the strany epic.
- mypy on the touched packages, because the cast strip is only right if mypy
  says so.
- Flaky-under-parallelism tests re-run serially before being attributed to
  the change.

Write the numbers into the ticket with the commit they were measured on.

## 13. Phase 9 — land

- Merge `--no-ff` onto the release branch from the shared checkout, push, and
  run the merged-tree suite the same way as phase 8. A sibling branch merged
  in the same window gets its own merged-tree suite.
- Deconflict with any agent holding a related ticket before merging its work;
  a silent holder is not consent, so record the attempt and the operator's
  power-of-the-pen grant on the ticket.
- Deploy with the exact restart form in AGENTS.md, then check the system
  status endpoint and the session-schema epoch.

## 14. Phase 10 — live trial

The seam is about behaviour at the boundary, so the closing evidence is live:

- Run scenarios from the composer standard battery
  (`evals/composer-standard-battery/battery.md`) plus one scenario designed to
  hit the seam (on the first seam: a candidate that needs a multi-error
  repair).
- Drive it through the API, not a browser profile a sibling may hold. Long
  composer turns outlive a tool call and the server cancels a compose when
  the client disconnects, so send them detached with a generous timeout and a
  done marker.
- Count what the seam is meant to change: repair turns used, tool calls per
  transition, terminal notices emitted, unknown-key placeholders reaching the
  model. A green pipeline with the wrong counts is not a pass.
- Record pass / fail per scenario, the counts, and the deployed commit on the
  ticket.

## 15. Phase 11 — close

- Close the ticket with the merge commit as the close commit and the phase 8
  and 10 numbers in the reason.
- Follow-ups found during the lane are fixed in the lane. A ticket for work
  the lane could have done is deferred waste. Only operator-decision items
  (signing, custody semantics, product doctrine) are ticketed.
- Update memory and, if a trap was new, `docs/agents/recent-code-hints.md`.

## 16. Definition of done

- [ ] Census by AST, counts on the ticket, orientation grep labelled as such
- [ ] Matrix with a verdict on every row
- [ ] Operator ratification per row, recorded
- [ ] Whole-tree gate deriving every side, with walker probes and a gated fence fixture
- [ ] Structural close (closed type + nominal runtime admission) where the type system can carry the invariant
- [ ] Three reviewer sign-offs on one named commit, all findings fixed and signed off by their originator
- [ ] Mutation ledger with no survivors
- [ ] Full suite exit 0 on the branch and on the merged tree, lint corpus delta and mypy recorded
- [ ] Merged, pushed, deployed, epoch checked
- [ ] Live trial with per-scenario results and seam counts
- [ ] Ticket closed against the merge commit; follow-ups fixed, not parked

## 17. Effort shape of the runs so far

For sizing a nomination "for the same effort". Two runs is not a trend, but it
is enough to show that "the same effort" is not a safe assumption: the second
seam was roughly six times the first on every axis that costs time.

| Item | First seam (planner rejection) | Second seam (tool-result envelope) |
|---|---|---|
| Distinct keys adjudicated | 64 (63 teach, 1 fence) | 363 (361 teach, 2 fence) |
| Shipped rows in census | 245 | 401 |
| Commits on the branch | 11 | 70 |
| Reviewer seats | 3 | 3 |
| Review rounds to zero | 5 | 6, plus 2 confirmation passes |
| Findings fixed | 1 major, 5 minor | 3 major, 6 minor in the last two rounds; earlier rounds not tallied |
| Live scenarios | 3 battery + the seam scenario | pending at merge |

Say which "distinct" a count means. The second seam's census admits three
readings that differ by 50: 401 rows are distinct
`(surface, tool, key, site)`; **363** — the figure the ticket reports — are
distinct `(surface, tool, key)`; 315 are distinct `(surface, key)` and 311 are
distinct key names. A bare "distinct keys" number is ambiguous enough that two
readers will reconcile different totals.

## 18. Traps met on the runs so far

First-run traps are below; the second run's are in §19.

- Ticket comments fail with a conflict when the holder actor differs from
  yours; check the holder before writing.
- The scratchpad directory is shared by every lane in the session; measure
  into a lane-private subdirectory.
- `pkill -f` with a literal from your own command matches your own shell.
- Edit scripts anchored on text the formatter has since rewrapped abort
  silently; re-read the region first.
- A bundled shell command that mixes merge, push, and status is what the
  permission classifier blocks; split into single-purpose calls.
- Verify a "deviation" against the plugin's capability tags before calling
  it one. Reference join is the lookup plugin.

## 19. Traps met on the second run

- **A gate file accumulates its own private tree walk.** Four separate
  `rglob("*.py")` walks of the composer package landed across one review round,
  each added by a different fix, and six review rounds did not see them — the
  full suite did, through `test_python_file_walker_authority`. Enumerate with
  `iter_gate_sources` from `tests/helpers/tree_gate.py`, and note that the
  authority greps raw file TEXT, so the helper's own docstring must not spell
  the call it replaces.
- **Closing a loose type turns a sibling branch's new code into a merge defect
  that git reports as a clean auto-merge.** When the deliverable is narrowing
  `Any` to a closed union, every call site written against the old type on
  another branch is broken by the merge, and no text-level tool can see it. The
  pre-commit mypy hook is the detector. Budget for one such repair per merge,
  and run mypy before trusting a conflict-free merge.
- **Verify a contract a comment asserts before relying on it.** The repair
  above rested on a comment claiming every `success=False` result carries a
  Mapping with an error key. It was true — two constructions exist and both do
  it — but that was worth thirty seconds to confirm rather than inherit.
- **A review round can converge on rewriting rather than deleting.** Four
  consecutive rounds ran seat-finds-false-sentence, fixer-rewrites,
  next-round-finds-a-new-defect-in-the-rewrite. Brief fixers to DELETE a false
  clause: absent teaching is a known gap, wrong teaching is a defect. Tell
  seats that a true sentence they would word differently is not a finding.
- **A guard owes a probe per cell where its predicate enumerates a FINITE
  space and the unwitnessed direction fails OPEN.** Fail-closed cells are
  witnessed by live code; fail-open ones never are, so they need a written
  probe or they are simply untested.
- **A hand-maintained count is the shape that keeps coming back.** Four
  instances in one lane: a set-keyed AST inventory that hid multiplicity, a
  prose "twelve routes" comment, a running byte ledger whose arithmetic did not
  close, and a docstring count the derivation already covered. Key on a count,
  or derive it, or delete it.
- **`.git` is a FILE inside a worktree**, so `ls .git/MERGE_HEAD` fails and
  reads as "the merge aborted" when it did not. Use `git rev-parse --git-dir`.
  A failed pre-commit hook leaves MERGE_HEAD and the staged merge intact.
- **`ruff format` runs in CHECK mode in pre-commit** — it blocks the commit
  rather than fixing the file. Run it yourself after any hand-written
  multi-line call, which is separate from the post-edit hook that STRIPS an
  import added before its first use.
- **Never match test processes by pattern to kill them.** One `pgrep -f`
  destroyed two sibling sessions' suite runs. Capture the PID at launch and
  verify `/proc/<pid>/cwd` is inside your own worktree.
