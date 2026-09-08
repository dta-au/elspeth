---
name: explore-and-pin
description: >
  The technique for closing a soft seam — a place where one side of the code ships
  a set of keys or facts, another side admits or consumes them, and a third side
  teaches a reader (an LLM, an operator, a custody walker) what they mean, with
  nothing but a `Mapping[str, Any]`, an allowlist and prose holding them together.
  Use when a composer tool, envelope, teaching text, redaction manifest, frontend
  decoder or guard is found to drift from its authority; when a guard turns out to
  be enforced only by naming or prose; when a knob the planner can set is not
  provably wired to every surface that carries it; or when nominating the next
  seam of the composer-tool campaign. Harness-neutral: no lanes, seats-as-agents,
  or merge protocol are part of the technique.
---

# Explore-and-pin

Closing a producer / admitted / taught seam so it cannot silently reopen. Executed
three times on the composer before this sheet existed (planner rejection feedback,
the tool-result envelope, argument wire parity) and once as a sprint over the web
subsystem's fail-open guards. The long-form record with per-run costs and traps is
`docs/agents/explore-and-pin-methodology.md`; this sheet is the technique.

The output is never a cleaner file. The output is a seam that cannot drift again
without a whole-tree test going red, and where possible a type that makes the
drift unwritable.

## 1. The seam model

A seam has sides that are maintained in different files by different mechanisms:

| Side | What it is | Typical form |
|---|---|---|
| SHIPPED | what the producer actually emits | TypedDict / dataclass fields, raise-site fact dicts, a json-schema property |
| ADMITTED | what the consumer accepts before it acts | redaction manifest, allowlist tuple, custody walker arms |
| TAUGHT | what a reader is told the keys mean | skill prose, tool descriptions, docstrings |
| MODEL | the type the handler validates against | pydantic arguments model |
| READ | what handler code actually consumes | `args["k"]`, `args.get("k")` |
| FRONTEND | where the value reaches the UI | TypeScript decoder / projection |

A composer tool **knob** (one argument the planner can set on one tool) has all six.
A knob is CONNECTED only when every wire that should carry it agrees. Each
disconnection has a symptom, and the ones with no error signal are the worst
because the model cannot learn from them:

| Disconnection | Symptom |
|---|---|
| SHIPPED not READ | a knob that does nothing; the model believes it authored something it did not |
| READ not SHIPPED | a hidden knob the model is never told about |
| SHIPPED not ADMITTED | the audit trail destroys a legitimate key NAME |
| TAUGHT not SHIPPED | burns a repair turn against a budget of exactly 2 |
| SHIPPED not FRONTEND | an approval card that shows nothing, or the wrong thing, to the one operator who asked to review |

This is not a typing problem. The founding instance (`description` on
`upsert_node` / `set_output`) involved no `Any` anywhere: both sides were correctly
typed tuples that disagreed. The criterion is "is the knob wired", never "is the
annotation precise". A `dict[str, object]` rewrite satisfies a checker and tells
the model nothing; the soft-mapping census (`scripts/check_contracts.py`) counts it
as a swap, not a retirement, on purpose.

## 2. When a seam qualifies

All four must hold, and the ticket names each:

- **Multiple sides** maintained separately.
- **Loose typing at the join** on at least one side (`Mapping[str, Any]`, `Any`, an
  open JSON column, a hand-written tuple mirroring a schema).
- **A trust boundary**: the bytes cross into the LLM, a custody walker, an export,
  or a subsystem that acts on them without being able to ask the producer.
- **A live consequence**: drift has produced, or plausibly produces, a wrong planner
  turn, a custody refusal, a fail-closed placeholder reaching a reader, or a
  silent loss. No consumer acting on the drift means a typing chore, not a seam.

Search the tracker first for prior adjudication of the same keys; a ruling in a
signed allowlist rationale carries forward.

## 3. Explore: the census

One row per shipped key, derived from source by AST or by importing the live
object. Never by regex over files: a grep number is orientation and is labelled
so on the ticket; it is never written into a claim.

- Producer side: TypedDicts and dataclasses reachable from the producer crossed
  with the keyword arguments each construction site passes, recursing through
  nested TypedDicts and `list[TypedDict]`; raise sites of the owned exception,
  reading literal dict keys and REFUSING to count a site whose facts are not a
  literal (a variable, comprehension, `**` splat, `cast`) — those become findings;
  production registries that already exist, imported rather than retyped.
- Consumer and teaching sides: quoted leaf names in the prose, keys in the
  allowlist, arms in the walker, properties in the json-schema — again by AST or
  import.
- The matrix joins the sides. Every row lands in one state: in sync; shipped but
  untaught; taught but unshipped; shipped but not admitted.
- Record the counts on the ticket with the commit they were measured on, and say
  which "distinct" a count means (`(surface, tool, key, site)` is not
  `(surface, key)`; the second run's census admitted four readings that differed
  by 50).
- Run it where `elspeth.__file__` and `elspeth_lints.__file__` resolve into the tree
  you mean (AGENTS.md worktree rules). A census that imported the wrong checkout is
  confidently wrong, not an error.

Expect the census to disagree with what anyone remembers: the first seam's commit
message claimed 27 keys; the census found 64.

## 4. Verdicts and ratification

Each untaught, unadmitted or stale row gets exactly one verdict:

- **teach** — write the entry. True against the code, not persuasive; name the
  key, its value shape, and what the reader should do with it.
- **fence** — shipped and staying untaught for a stated reason (internal, the
  reader cannot act on it, on its way out). A fence is data in a fixture with its
  reason, the gate reads it, and the count is capped. A growing fence is the seam
  reopening under another name.
- **fix the producer** — the row should not exist in that shape: ship a closed
  label, split an overloaded key, or stop shipping it.
- **retire** — taught but never shipped: delete the prose and the test that pinned it.

Two rules from the first run: one prose slot per code (if one rejection code ships
two fact shapes, teach both in the one entry or split the code); and a terminal
rejection is a verdict of its own (if no candidate the reader can author clears
it, the feedback says so and tells the reader to decline in plain text, keyed on
`(code, exact fact-key shape)` so the planner stops repairing instead of looping).

The LLM charter reviews the teach rows against real wire samples BEFORE they go
to the operator (load `yzmir-llm-specialist:using-llm-specialist`; §8 says what to
hand it), and the systems charter runs the shape-propagation sweep in parallel
(`yzmir-systems-thinking:using-systems-thinking`, pattern-recognizer): every faulty
shape the census surfaced is searched for tree-wide and lands in a shape ledger
that rides with the walkthrough, each site closed here or fenced under a named
ticket. Changed verdicts carry the seat's reason.

Before the verdicts are treated as authoritative, walk the operator through them
as a table (key, site, verdict, one line of reason), fences and producer fixes
first because those are the decisions. Ratification is per row and is recorded on
the ticket with the date. A silent operator is not consent.

## 5. Pin: the gate

A whole-tree unit test. Each rule was learned by losing to it once:

1. **Derive, never enumerate.** Every side is computed from source at test time.
   A hand-listed set is a second copy of the seam and drifts exactly like the
   first. If a list must stay hand-maintained (a TypeScript switch Python cannot
   read), it is the single declared exception, it has its own typo-catching test,
   and the gate's docstring says what it cannot see.
2. **Refuse the escapes.** The walker rejects, with a named reason, every construct
   that would let a producer ship a fact it cannot read: dict literals or
   comprehensions inside a fact value, `cast(...)` in a fact value, a starred splat
   into an owned constructor, a constructor called outside a recognised site, an
   aliased constructor.
3. **Attribute by ownership, not by keyword.** A site is a call to an owned
   constructor (class name read from the class object), a module-level alias of
   one, or a `replace(...)` carrying its keyword. Keying on the keyword alone sweeps
   unrelated constructors.
4. **Pin the walker with probes.** Module-level probe TypedDicts and probe sites
   exercise every walker branch, including nested and list-of-TypedDict recursion.
   The first probe set found a dead branch the gate had been passing over.
   Function-local probes do not work under postponed annotations.
5. **The fence is a fixture and is itself gated.** The gate fails on an unfenced
   untaught row AND when a fenced row becomes taught or stops shipping.
6. **Walk the whole owned root**, not the files you touched. Enumerate with
   `iter_gate_sources` from `tests/helpers/tree_gate.py`; a gate that grows its own
   `rglob` walk is caught by `test_python_file_walker_authority`.
7. **A guard owes a probe per cell** where its predicate enumerates a finite space
   and the unwitnessed direction fails OPEN. Fail-closed cells are witnessed by
   live code; fail-open ones never are.
8. **Compare the right pair of wires.** A gate that checks SHIPPED against MODEL
   for two tools reads as covering the knob and passes beside a live defect on
   SHIPPED against ADMITTED. Enumerate the wires so an unchecked pair cannot be
   overlooked; extend the existing authority rather than add a parallel one.

Name the test by what it certifies and put the reason for each refusal in the
assertion message; the next reader learns the rule from the red.

## 6. Close structurally

Every walker refusal is a syntactic close: it stops one way of writing an
unreadable fact. Wherever the language can hold the invariant, move it there and
let the walker become a backstop:

- Give the value a closed type (`GuidedFactValue = str | int | bool | None | list[str]`).
- Admit it nominally at runtime in the owned constructor per ADR-032: exact-type
  checks, no `isinstance` on subclasses, copy any list, raise the audit-integrity
  error naming the key and the offending type.
- Strip the `cast(JsonValue, ...)` calls the loose type had forced; mypy says which
  survive (the first seam removed 48, kept 5).
- Pin the type with a test that refuses each disallowed shape and admits each
  allowed one, including copy semantics.
- Land the structural close first, then simplify the walker to what the type
  cannot express. Do not carry both indefinitely.

Closing a loose type turns another branch's new code into a merge defect that git
reports as a clean auto-merge; the pre-commit mypy hook is the detector.

## 7. Soft guards

A guard is SOFT when it is enforced only by naming or prose rather than by
structure. The web-subsystem sprint found the recurring shapes:

| Shape | Example | Fix |
|---|---|---|
| assertion that cannot fire | `A & (B - A)` disjointness check | delete it and the prose claiming it polices anything; cite the real pin |
| hand-listed inventory beside a live one | a list of response models to check for `strict=True` | derive the inventory from the live module (`vars(schemas)`) with a non-empty positive control |
| docstring scope wider than the registry | "grounds `plugin` and `on_success`" with no pattern for either | narrow the text to the enforced set; delete the dormant arms |
| verifier reach implied by naming | a claim extractor with no reader for a field it extracts | a reader REGISTRY; an extracted field with no reader is an import-time error, not a silent skip |
| comparison by spelling where the binding is by identity | blob ids compared as strings while `UUID()` binds either hex case | compare through the contract helper (`names_same_blob`) at every walker |
| public method that manufactures output for a state its predicate rejected | `build_hint()` callable without `should_fire()` | one authority returning `X | None`; the other derives from it or raises |

The two admissible fixes are the same in every row: make the guard derive from its
real authority, or delete it. Rewording a false claim is not a fix; absent
teaching is a known gap, wrong teaching is a defect. Never add aliases, padding or
dead code to hold a signature or a pin still.

## 8. Review to zero

Three charters, each reading the tree, each written against one named commit:

- **Adversarial**: disprove the gate — ship a fact it does not see, find a test
  that passes for the wrong reason, revert a fix and watch its test survive.
- **LLM**: read the seam from the model's side against real wire samples. Is each
  teaching entry actionable from the bytes the model actually receives; does the
  terminal notice stop a loop rather than describe one.
- **Systems**: which sibling seams share the shape (a shape ledger, every site
  named, each closed here or fenced under a named ticket); what the new registry or
  type is now the authority for; what the next seam should be.

Every fix goes back to the charter that raised it. A true sentence a reviewer would
word differently is not a finding. The go is three written sign-offs on one commit,
and a verdict never travels across a rewrite of the function it rested on.

Which skill or agent carries each charter (operator ruling 2026-09-02: the LLM
and systems charters are first-class reviewers at verdicts, review and close-out,
not final-gate add-ons):

| Charter | Load / dispatch | What it brings to this technique |
|---|---|---|
| Adversarial | the `red-team` agent, with the gate file and the commit as its attack surface | mutation survivors, tests passing for the wrong reason, escapes the walker cannot see |
| LLM | `yzmir-llm-specialist:using-llm-specialist` (routes to the prompting, agentic/MCP tool-use, context-engineering and LLM-as-judge sheets); `yzmir-llm-specialist:llm-diagnostician` for a live transcript that went wrong | whether a taught sentence is actionable from the wire bytes, whether a terminal notice stops a loop, what the model can do after the seam closes that it could not before |
| Systems | `yzmir-systems-thinking:using-systems-thinking` (routes to `analyze-system`, `find-leverage-points`, `map-dynamics`); `yzmir-systems-thinking:pattern-recognizer` for the shape ledger, `leverage-analyst` when choosing the next seam | the shape-propagation sweep, which sibling seams share the defect's mechanism, what the new registry or type is now the authority for, and which seam to nominate next |

Both routing skills expect a stated question: give the LLM seat the real
serialized samples and the teaching text, give the systems seat the census's
faulty shapes and the tree root, and ask each for a written verdict on the named
commit. Neither seat writes code in the review round.

## 9. Mutation ledger

Mutate the gate and the structural close, not the defect that motivated them:

- For each guard, apply one mutation that should turn it red (drop a refusal arm,
  widen the type, remove a fixture row, restore an early return), run, restore
  from a saved copy (never `git checkout --` of the path).
- Verify the mutation string was found before counting a result; a replacement
  that matched nothing reports a false survivor.
- A mutation result is evidence only once the mutated and unmutated runs executed
  the SAME test selection: reconcile failed + passed against the baseline total.
  Three harness failures in one day looked like ordinary evidence until that
  check (a reporter flag that did not exist, a fixture faithful in values but not
  in key order, a mutant run over a different selection).
- Record every mutation, whether it applied, and the exit code on the ticket. A
  survivor is a finding for §8.

## 10. Evidence and the bar

- Full suite from a file, exit code read from the process, never a pipe; compare
  the failing SET to the base commit's set, and re-run parallelism-flaky ids
  serially before attributing them.
- Trust-tier lint corpus in shape-only verify mode, compared to the base commit's
  corpus with positions masked, never to zero.
- `scripts/check_contracts.py` (contracts + soft-mapping census); a Mapping rewrite
  is a swap and must not be reported as a retirement.
- mypy on the touched packages.
- Live trial through the API, counting what the seam was meant to change (repair
  turns, tool calls per transition, terminal notices, unknown-key placeholders
  reaching the model). A green pipeline with the wrong counts is not a pass, and a
  reproduced peer failure is data, not a fail.

Follow-ups the work could have done are done in the work; only operator-decision
items (signing, custody semantics, product doctrine) become tickets. Residue that
is deliberately left is recorded on a ticket with its measurement, never in a
docstring alone.

## 11. Definition of done

- [ ] Census by AST with counts and their "distinct" reading on the ticket
- [ ] Matrix with one verdict per row; ratification per row recorded
- [ ] Whole-tree gate deriving every side, walker probes, gated fence fixture
- [ ] Structural close where the type system can carry the invariant
- [ ] Three charter sign-offs on one named commit, each finding closed by its originator
- [ ] Mutation ledger with no survivors, selections reconciled
- [ ] Suite set-diff, lint corpus delta, census delta, mypy — all with the commit
- [ ] Live trial counts recorded
- [ ] Follow-ups fixed; deliberate residue on a ticket with a measurement

## 12. Where the authorities live today

- Response envelope: `tests/unit/web/composer/test_tool_result_envelope_gate.py`
  + `tool_result_envelope_fence.json` (SHIPPED / ADMITTED / TAUGHT for every key
  `ToolResult.to_dict` ships).
- Argument knobs, SHIPPED against ADMITTED: `test_tool_argument_wire_parity.py`
  (live `get_tool_definitions()` in `tools/_dispatch.py` against
  `MANIFEST[tool].policy.known_argument_keys` in `redaction.py`).
- Argument knobs, SHIPPED against MODEL: `tools/schema_contract.py` (two tools only).
- Planner repair-feedback facts: `test_planner_teaching_gate.py` + `planner_teaching_fence.json`.
- Frontend decoding of redacted arguments: `frontend/src/utils/redactedArguments.ts`,
  fixture `frontend/src/test/fixtures/redacted-tool-arguments.json` generated by
  `scripts/cicd/bootstrap_proposal_diff_fixture.py` and pinned (values AND key
  order) by `test_proposal_diff_redaction_fixture.py`.
- Soft-mapping census: `config/cicd/soft-mapping-census.yaml` via `scripts/check_contracts.py`.
- Knob-to-wire criterion and its open instances: epic `elspeth-54bd0b84cd`.
