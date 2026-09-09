# ADR-039: Unconstrained Text Framing — A Positive Claim That Makes Generative Producers Gateable

**Date:** 2026-08-07
**Status:** Accepted
**Deciders:** ELSPETH maintainers
**Tags:** contracts, plugin-semantics, composer-authoring, llm, extends-adr-008

## Context

A user asked the Composer to generate an announcement and write it to a text
file. That request had **no correct answer** (Filigree `elspeth-afdf55a17c`,
"g11"):

1. `llm -> text` diverts every multiline value and publishes nothing. `TextSink`
   rejects any value containing CR or LF, by design — it guarantees one input
   row maps to exactly one output record. The run "succeeds" with a 0-byte file.
2. `llm -> line_explode -> text` — the composition that *does* work — was
   **refused at authoring time**.
3. No sink writes an unframed multiline value to a file (`text` diverts, `csv`
   quotes under a header, `json` escapes to a literal `\n`).

The runtime is correct throughout. Every defect was in authoring.

### Why the obvious fix was provably inert

The obvious repair is to declare `TextSink`'s single-line requirement in the
semantic-contract layer so the Composer refuses `llm -> text`. Executed against
the real declared facts, that is **inert**:

```
llm -> text [{COMPACT, LINE_COMPATIBLE}] : unknown -> advisory only
llm -> text [{COMPACT}]                  : unknown -> advisory only
```

Both LLM plugins declared `text_framing=UNKNOWN`, and `compare_semantic` maps an
`UNKNOWN` fact to an `UNKNOWN` outcome on that dimension **before** any
membership test runs. So `CONFLICT` was unreachable for any LLM producer under
any requirement and any `unknown_policy`.

That is the general result, and it is the reason this ADR exists:

> **A producer that abstains cannot be gated.** `UNKNOWN` means "nobody
> declared". Its severity is decided by the *consumer's* `unknown_policy`, never
> by the facts — so it can be downgraded to an advisory, but it can never be a
> contradiction. No amount of consumer-side strictness makes an abstaining
> producer conflict.

And the abstention was **honest**. You cannot statically know whether a model
emits a newline. `UNKNOWN` was not a declaration bug to be corrected; the
vocabulary simply had no way to say the true thing.

### What the true thing is

The LLM plugins know something real and stronger than "no idea": the value is
**free text whose framing is not statically decidable, under any
configuration**. That is a positive property of the producer, not an absence of
information. `web_scrape` can say `NEWLINE_FRAMED` or `COMPACT` because its
`format`/`text_separator` options settle the question. A generative producer's
framing is settled by nothing.

## Decision

### 1. `TextFraming.UNCONSTRAINED` — a positive claim, not an abstention

Add one member to the closed `TextFraming` vocabulary
(`contracts/plugin_semantics.py`):

| member | meaning | `compare_semantic` treatment |
| --- | --- | --- |
| `UNKNOWN` | ABSTENTION — nobody declared | short-circuits to an UNKNOWN outcome; graded by `unknown_policy`; **can never CONFLICT** |
| `UNCONSTRAINED` | CLAIM — the value is free text and its framing is not statically decidable | ordinary set membership; **CONFLICTs** against any requirement that does not accept it |

The distinction is the entire mechanism. Because `UNCONSTRAINED` is a real
member it is compared by membership like every other member, so a consumer that
does not accept it gets a hard `CONFLICT` — independent of `unknown_policy`,
because `compare_semantic` short-circuits `CONFLICT` ahead of `UNKNOWN`.

**This is what makes `llm -> text` hard-blockable.** A later phase — landed in
`657d12b42`, the commit after this one — gives
`TextSink` `accepted_text_framings=frozenset({TextFraming.COMPACT})`;
`UNCONSTRAINED` is not in `{COMPACT}`, so the composition becomes a
policy-independent authoring error. Verified:

```
llm(text_framing=UNCONSTRAINED) -> {COMPACT}-only requirement : conflict
```

That gate is unreachable while the producer abstains. Prevention required the
producer to make a claim.

### 2. Both LLM plugins declare it; `content_kind` deliberately does not change

`plugins/transforms/llm/transform.py::output_semantics` (both the
`MultiQueryStrategy` and single-query branches) and
`plugins/sources/llm/source.py::output_semantics` declare
`text_framing=UNCONSTRAINED` for their raw response fields.

`content_kind` **stays `UNKNOWN`**, and the asymmetry is the principle, not a
compromise:

> Make the positive claim **only** on the dimension where you want `CONFLICT` to
> become reachable.

A positive claim converts abstention into reachable conflict on that dimension —
in both directions. On `text_framing` that is exactly the goal. On
`content_kind` it would be a defect: an LLM asked for markdown really does emit
markdown, so a blanket `content_kind` claim would manufacture false conflicts
against every consumer constraining that dimension. Prose-versus-markdown is a
genuine per-response unknown, and `UNKNOWN` is the honest declaration for it.

Declaring `UNCONSTRAINED` as a shortcut for "I did not look" re-creates the
abstention it exists to replace, and is a declaration lie in the ADR-014 sense.

### 3. `line_explode` accepts it, constrains framing only, and drops to WARN

Three coupled changes to `line_explode`'s requirement, which together make the
correct composition authorable:

**(a) Accept `UNCONSTRAINED`.** Splitting unconstrained free text is legitimate:
it is how generated multiline text becomes one row per line, which is the only
correct way to write it to a file.

**(b) Constrain framing only —`accepted_content_kinds` becomes empty**
(the shape `json_explode` already uses). `splitlines()` cares where the line
boundaries are, not what the text *means*. The old `{PLAIN_TEXT, MARKDOWN}`
constraint blocked nothing extra — every producer it should block conflicts on
framing alone (`web_scrape`'s compact text declares `COMPACT`; its raw html
declared `NOT_TEXT` when this ADR was written, but that was a false claim
about a str of HTML and was re-declared `UNCONSTRAINED` in `800b4887a`,
elspeth-24c04df25f, making raw legitimately splittable rather than blocked)
— while downgrading to `UNKNOWN` every producer that
declares framing but honestly abstains on kind. It was also **wrong** in one
real case: `JSON_STRUCTURED` + `NEWLINE_FRAMED` is JSONL, and splitting JSONL
into one object per row is correct rather than a defect. `NOT_TEXT` remains the
member that carries "no line operations on this"; `content_kind` never did.

**(c) `unknown_policy` FAIL -> WARN.** `line_explode` is a **usefulness** guard,
not a correctness one: compact text yields a single row holding the whole value
— a no-op, not data loss. Contrast `TextSink`, which *discards* the row, and so
earns a hard requirement. Nothing genuinely wrong is unblocked, because a wrong
producer declares a conflicting framing and `CONFLICT` outranks every policy.
`FAIL` was blocking exactly one class — producers that did not **declare** —
which is what refused the repair (`elspeth-b6d9f04827`).

Resulting outcome table, verified by execution against the real plugin objects:

| producer -> `line_explode` | before | after |
| --- | --- | --- |
| `web_scrape` compact text | conflict | **conflict** (still blocked) |
| `web_scrape` raw html | conflict | **satisfied** (since `800b4887a`: raw declares `UNCONSTRAINED`, elspeth-24c04df25f) |
| `web_scrape` newline-framed | satisfied | satisfied |
| `web_scrape` markdown | satisfied | satisfied |
| `llm` transform | unknown -> **error** | **satisfied** |

The repair is no longer merely tolerated by a relaxed policy; it is a positive
factual match.

### 4. The ordering constraint is load-bearing

> **`line_explode` must be unblocked BEFORE anything hard-blocks `llm -> text`.**

Blocking `llm -> text` while `llm -> line_explode` is still refused would leave
the user's goal — write generated text to a file — with *no* expressible
spelling at all, and would convert a silent empty file into a flat refusal.
That is worse: the user loses the outcome and gains nothing.

Done in this order, the block merely redirects an author to the robust form that
already works. §3 is therefore a prerequisite of the `TextSink` requirement, not
a companion to it, and this ADR is only half-realised until that requirement
lands.

> **Update (`657d12b42`, same branch):** that requirement has since landed, in
> the order this section requires — §3 first, the `TextSink` gate second — so
> the ADR is now fully realised rather than half. The ordering constraint was
> honoured, not bypassed.

### 5. Why adding a positively-claimed member is safe today

At the time of this decision, exactly two consumers declare
`input_semantic_requirements()` in the whole tree, so the blast radius is a
closed enumeration rather than an estimate (the `TextSink` requirement of §4/§6
is the expected third, and it is designed to reject the new member):

| consumer | `accepted_text_framings` | effect of the new member |
| --- | --- | --- |
| `line_explode` | accepts `UNCONSTRAINED` (§3) | `llm` producers become SATISFIED |
| `json_explode` | `frozenset()` — dimension unconstrained | dimension skipped entirely; no change |

No third consumer can be surprised by the addition because no third consumer
exists. A future consumer that constrains `text_framing` must decide explicitly
whether unconstrained free text is acceptable to it — which is the point.

> **Update (`657d12b42`, same branch):** the enumeration above is the state on
> the day of the decision and is retained as such; the live consumer set is now
> four. `TextSink` arrived as the anticipated third and rejects `UNCONSTRAINED`
> (`{COMPACT}` only), and `DocumentSink` arrived as a fourth that accepts it.
> Both made the explicit decision this section asks a new consumer to make,
> which is the mechanism working rather than an exception to it.

### 6. Non-goals

- **No change to `compare_semantic`.** `UNCONSTRAINED` is deliberately an
  ordinary member with no special-casing; special-casing it would recreate the
  ungateable-abstention behaviour this ADR removes.
- **No `ContentKind` sibling member.** See §2 — it would be actively harmful.
- **`TextSink.input_semantic_requirements()` is not added here** (§4 ordering),
  nor the `BaseSink`/`BaseSource` hooks and sink-producer walk-back that reading
  a sink requirement would first require.
  **Update (`657d12b42`, same branch):** all of it landed in the very next
  commit — both hooks, the sink-producer walk-back, and the `TextSink`
  requirement itself. This bullet scopes *this decision*; it was never a
  standing prohibition, and §4 was scheduling that work rather than forbidding
  it.
- **Authoring surface only.** `validate_semantic_contracts` is reachable only
  from `web/` — a hand-authored YAML `llm -> text` pipeline gets nothing from
  this. Making the guarantee uniform means expressing it as an ADR-010
  `boundary_check` runtime adopter instead, which fires on the actual value; a
  larger change and a different ADR.

## Consequences

### Positive

- "Generate text and write it to a file" has a correct, authorable spelling for
  the first time: `llm -> line_explode -> text`, SATISFIED rather than merely
  tolerated.
- Generative producers become gateable at all. The `CONFLICT` that hard-blocks
  `llm -> text` is now reachable, where it was provably unreachable before.
- One latent false conflict removed: JSONL into `line_explode` (§3b).
- The vocabulary now distinguishes "nobody declared" from "declared to be
  unbounded" — two facts that were previously indistinguishable and carried
  opposite correct treatments.

### Negative

- `line_explode` no longer hard-refuses a producer that declares nothing at all;
  it warns. That is the intended trade (§3c), but it does mean an undeclared
  producer feeding compact text now reaches the runtime, where it degrades to a
  one-row no-op rather than an authoring error.
- Dropping `accepted_content_kinds` gives up a hypothetical block on a producer
  declaring `BINARY` with a line-bearing framing. No producer declares `BINARY`
  today, and `NOT_TEXT` is the designed carrier of that block — but the
  theoretical hole is real and named here rather than left implicit.
- One more closed-vocabulary member to reason about, and a distinction
  (`UNKNOWN` vs `UNCONSTRAINED`) that a careless declarer can get wrong in the
  direction of over-claiming.

### Neutral

- No DB schema, wire-format, or runtime-behaviour change: this is authoring-time
  vocabulary. Existing pipelines are unaffected.
- `plugins/sources/llm/source.py::output_semantics` was **unreachable** from the
  composer's semantic validator **when this decision was taken**: the validator
  had no typed route to construct a source and ask it (`BaseSource` had no hook;
  the probe knew only `create_transform`; ADR-032 forbids bridging that with
  duck-typing). The declaration was written correct-but-unread, against the hook
  landing later. The llm **transform** was the only reachable generative
  producer on that day.

  **Update (`657d12b42`, same branch): it is now live, and this ADR's original
  wording no longer describes the tree.** Adding the sink requirement of §4
  required precisely the missing plumbing, so the same commit added
  `BaseSource.output_semantics()` as a **declared base method** — deliberately
  not a `hasattr` bridge, which is what ADR-032 rules out — together with
  `_instantiate_source_producer` in `web/composer/_semantic_validator.py`, which
  probes through `create_source` rather than `create_transform`. A source-fed
  sink edge therefore reads the source's own declared facts, and
  `llm source -> text` is a hard CONFLICT on the source's `UNCONSTRAINED` claim,
  exactly as the transform edge is. `TestSourceProducerFactsAreRead` in
  `tests/unit/web/composer/test_semantic_validator.py` pins that the probe
  really reaches `create_source`, so the gate cannot silently go inert again.

## Alternatives Considered

### 1. Keep `UNKNOWN` and tighten `TextSink`'s requirement

Rejected — provably inert; see Context. This was implemented and measured before
being abandoned. `CONFLICT` cannot be reached against an abstaining producer, so
the requirement changes nothing about `llm -> text` no matter how strict it is.

### 2. Relax `line_explode`'s policy alone, with no vocabulary addition

`FAIL -> WARN` by itself makes `llm -> line_explode` authorable (as an advisory).
Rejected as the whole answer: it leaves the outcome a *policy* question rather
than a *factual* one, keeps a permanent spurious warning on the correct
composition, and leaves `llm -> text` ungateable forever — so g11 stays
preventable only by prose. Retained as one part of §3.

### 3. Add a parallel `ContentKind.UNCONSTRAINED`

This would also make `llm -> line_explode` SATISFIED, without touching
`line_explode`'s `accepted_content_kinds`. Rejected: it makes every LLM producer
hard-CONFLICT with every present and future consumer that constrains
`content_kind`, including the true case of a model asked for markdown. §2's rule
— claim positively only where conflict *should* become reachable — decides
against it. Dropping the redundant, and in the JSONL case incorrect,
`content_kind` constraint is both smaller and more honest.

### 4. Have the LLM plugins declare `content_kind=PLAIN_TEXT`

Rejected as a declaration lie. Model output is frequently markdown; a false
`PLAIN_TEXT` claim errs in the *blocking* direction against a
markdown-constraining consumer, which is the more dangerous way to be wrong.

## Related Decisions

- **Extends:** ADR-008 (Runtime Contract Cross-Check) — the semantic-contract
  layer's abstention grading.
- ADR-032 (Validate by Trust Domain) — why the source-facts gap was never
  bridged with `hasattr`/duck-typing, and was ultimately closed with a declared
  `BaseSource` hook instead (§6, Neutral).
- ADR-014 (Schema Config Mode Contract) — declaration-lie framing used in §2.
- ADR-010 (Declaration Trust Framework) — the runtime `boundary_check` route
  named as the alternative for uniform, both-surface coverage (§6).
- Filigree `elspeth-afdf55a17c` (g11), `elspeth-b6d9f04827` (the refused
  repair), `elspeth-9595abb7b0` (diversion-reason disclosure, fixed).

## Implementation Notes

1. `contracts/plugin_semantics.py` — `TextFraming.UNCONSTRAINED`; the
   `UNKNOWN`-vs-`UNCONSTRAINED` distinction documented on the enum; module
   docstring records that additions must carry an ADR; `UnknownSemanticPolicy`
   docstring corrected (it claimed WARN/ALLOW were unused, false since
   `json_explode`).
2. `plugins/transforms/llm/transform.py` — both `output_semantics` branches.
3. `plugins/sources/llm/source.py` — same declaration; `-> Any` return
   annotation replaced with the real `OutputSemanticDeclaration`.
4. `plugins/transforms/line_explode.py` — §3(a)(b)(c); `get_agent_assistance`
   gains the generative-producer arm so the advisory says what to *do*.
5. `plugins/sinks/text_sink.py` — the multiline prohibition gains a remedy
   naming `line_explode`. A prohibition with no alternative is what produced
   g11.
6. `web/composer/_semantic_validator.py` — corrected module docstring (WARN
   guidance, and the false "exactly ONE walk-back" claim: `state.py` holds two
   more) and the false "sources do not expose `output_semantics()`" comment.
7. Tests — the outcome table of §3 pinned against the real plugin objects;
   closed-vocabulary membership pin updated; the three `line_explode` FAIL-era
   tests rewritten to assert the advisory channel.
