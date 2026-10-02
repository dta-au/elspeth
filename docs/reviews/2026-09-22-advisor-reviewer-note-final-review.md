# Final review — `fix/advisor-reviewer-note` (plan 3 of the advisor-block fix)

> **Disposition (executor, after the review).** Reviewed range
> `1efadb8f1..c8305590c`. Fix pass landed as `cbf145d6a` and `b8f9dda3b`:
>
> - **I-1 FIXED** — the note kept the verdict token for three of the four reply
>   shapes the parser documents as observed live. `_advisor_note_text` now finds
>   the lead line by line with the markdown emphasis matched *inside* the
>   pattern; pre-stripping the line (the reviewer's suggested form) deletes the
>   underscores from step ids and from the fence sentinels, which broke two pins
>   on the first attempt. Pinned over all seven shapes.
> - **I-2 FIXED** — control stripping now goes by Unicode category
>   (`Cc`/`Cf`/`Zl`/`Zp`, keeping tab and newline) in
>   `_strip_note_control_characters`, so bidi overrides, the zero-width family
>   and the BOM cannot make a rendered note differ from the stored one.
>   Expressed as categories and `chr()` code points, never literals — ruff
>   RUF001 rejects ambiguous literals, and an invisible character in source is
>   exactly what this code removes. U+2028/U+2029 arrive as line breaks
>   (`splitlines` honours them) and are kept as newlines rather than deleted:
>   stored and rendered then agree, which is the property the finding is about.
> - **I-3 FIXED** — the R2-F13 containment pin now parses a real verdict,
>   asserts *positively* that the words are in `blockers[].note` (so it can no
>   longer pass by avoiding the path), excludes only that field from the sweep,
>   and re-checks the serialised blob with the note's own text removed.
> - **I-4 RECORDED, code unchanged** — the deviation (three builders, not the
>   plan's four) is correct: `_advisor_signoff_pending_handoff_validation`
>   appends no advisor blocker by design (elspeth-66717f0c99), so there is no
>   row for a note to ride and the plan's `[handoff]` assertion could not have
>   passed. **Operator decision owed:** in that one preflight shape the user
>   still gets the bare "did not clear" message with no header and no note.
> - **I-5 FIXED** — the narrowed pin now asserts the note *is* in
>   `model_dump_json()`. No HTTP round-trip test was added: the route seam test
>   carries a real note and proves the route parses it and hands it to the
>   service, but that service is an `AsyncMock`, so a response-body assertion
>   there would prove nothing about merging.
> - **M-2 PROMOTED AND FIXED** — the reader rejected an empty note but no writer
>   forbade one, and such a row fails every uncaught `parse_completion_gates`
>   call: a bricked session, not a degraded one. Normalised at the writer.
> - **M-3 and M-1's blank-line half PROMOTED AND FIXED** — runs of three or more
>   newlines collapse, so removing a machine line no longer leaves a gap and a
>   newline-heavy note no longer pushes the row's button down the panel.
> - **Deferred:** M-1's CSS `max-height`, M-4 (the panel's contract comment),
>   M-5 (an unreachable parametrisation), M-6 (the withheld-twin sentence, which
>   is already open with the operator and which this branch makes more visible).
>
> **The gate caught what the review could not.** The full-suite gate on
> `c8305590c` failed with six epoch reds in three suites the Task 5 measurement
> grep never scanned (`tests/unit/docs`, `tests/unit/architecture`,
> `tests/unit/website`): README, CHANGELOG, the ECS scenario-B
> `structural_changes` record, and nine `schema.py` `line=` pins in
> `test_session_db_mutation_authority` — a line-only re-pin of `+3` caused by a
> three-line comment, proved line-only by every other field of every record
> being byte-identical. Root cause: the measuring grep was narrower than
> reality, and a too-narrow grep returns exactly what a correct one returns when
> there is nothing to find.
>
> **Final gate on `b8f9dda3b`** (`frozen=yes`,
> `/tmp/elspeth-gates/john/20260922T055547Z-advisor-reviewer-note-3067477`):
> ruff 0, mypy 0, contracts 0; lints `findings=2274`, unchanged from base with
> an empty position- and path-stripped diff; pytest 2 failed / 55681 passed,
> both reds the pre-existing `test_freeform_planner_failure_translation`
> failures that also fail on the untouched base.

# Final review — `fix/advisor-reviewer-note` (`1efadb8f1..c8305590c`)

Fresh-context, read-only code review. **No test suite was run** (the full-suite
gate owns the worktree). Every line number below is at `c8305590c`, read via
`git show c8305590c:<path>` from the main checkout; the gated worktree
`.claude/worktrees/advisor-reviewer-note` was never touched — no read, no
`git status`, no `git diff`, no write.

Reviewed in three passes: (1) the plan, the ledger and the two background
documents; (2) the eight commits' production diff plus the surrounding code at
head; (3) an adversarial pass over the sanitiser, the step-id validator, the
Tier-1 seam, the epoch pin set and the frontend render.

---

## Strengths

- **The containment architecture is the right shape.** Provider prose is
  bounded exactly once, at one function (`_advisor_note_text`,
  `service.py:11007`), before any row or surface exists; the *header* is chosen
  from a closed server-owned vocabulary (`_ADVISOR_CATEGORY_HEADERS`,
  `service.py:10810`) so the advisor picks *which* sentence but never its
  words; and the bounded note reaches exactly one wire field and one render
  site. That is a genuinely small attack surface for a feature whose whole
  point is publishing model output.

- **`_validated_advisor_step_ids` (`service.py:10821`) is safer than it needs
  to be, and correctly so.** Two independent constraints compose: the raw
  candidates come only from `_ADVISOR_STEP_ID_RE = [A-Za-z0-9_.\-]+`
  (`service.py:11001`), and each must then be `in` the live `known` set. So
  even if `state.outputs` held a name with a space, a quote or markup, no
  candidate token could ever equal it — the header is provably drawn from the
  intersection of a safe charset and real state. `set(state.sources)` is
  correct: `CompositionState.sources` is `Mapping[str, SourceSpec]`
  (`composer/state.py:336`), so its keys are the ids. Order-preserving
  de-duplication is right. **Verified clean.**

- **REQUIRED-with-no-default was the right call on both fields, not noise.**
  On the pydantic model (`schemas.py:259-262`) it converts "a builder forgot the
  decision" from a silent `None` into a construction error at 47 sites; on the
  Tier-1 envelope (`completion_gates.py:292`) it converts writer drift into a
  fail-closed read. The two are load-bearing in different ways and both earn
  the churn.

- **The epoch bump is complete where it matters operationally.** The coupled
  `_COORDINATION_HARD_CUT_EPOCH` moved with a reason comment
  (`sessions/schema.py:37-40`) — `schema.py` checks the two for *exact*
  equality, so a miss here is a startup failure, not a drift. Critically, the
  **operator verification probe** was found and updated:
  `docs/runbooks/staging-session-db-recreation.md:817`
  (`PRAGMA user_version; # expect 65`), as was
  `website/get-started.html:106`. I re-ran the search independently over
  `docs website src scripts deploy` at `c8305590c`: every surviving `64` is
  either a historical sentence that names its own change, a `64-bit` match, or
  the new "rows written at epoch 64 cannot be read forward" comment. The
  executor's Task-5 ruling (three sentences are history and stay) is **right**.
  **Verified clean.**

- **The CSI-before-C0 regex ordering fix is real, not cosmetic.** `ESC` is
  itself a C0 byte, so the plan's alternation order would have consumed the
  `ESC` alone and left `[31m` in the note. The executor caught it from a RED
  test and pinned it (`test_note_is_bounded_and_sanitised` asserts the exact
  output `"keepthis  and"`). That test is not vacuous — I traced it by hand and
  it fails under the plan's original ordering.

- **The AST rewrite was controlled, and the control caught real misses.** The
  ledger records before/after counts (47→0, 28→0), a known-negative file,
  `ruff format` unchanged and a full diff review. More convincingly, two
  independent failures were *caught and fixed*: the TS signature over-matched
  the `DecisionRow` blocker shape at three sites (reverted, ledger line 12),
  and a `toEqual` argument literal at `decisionPanelRows.test.ts:130` that
  `tsc` structurally **cannot** flag was caught by vitest (ledger line 14). I
  scanned every `+` line of the test diff mentioning `note` and found no
  misplaced insertion — every one sits inside a `ValidationReadinessBlocker(`
  call or a gate-fact dict. A wrong edit here fails loudly (required field), so
  the residual risk is a *missing* site, and a missing site is a construction
  error. **Verified clean.**

- **The Task-6 `decisionId` ruling is correct, and I checked its stated
  reason rather than taking it.** `decisionPanelRows.ts:160` builds the id from
  `[blocker.code, blocker.component_id, blocker.detail, blocker.suggestion]` —
  `detail` *is* in the id, so the executor's rationale ("detail already moves
  when the verdict does") holds: a different category or step list changes the
  header, changes `detail`, changes the id. And `decisionId` appends an
  occurrence counter, so even two byte-identical rows cannot collide on a React
  key. Excluding `note` costs nothing and churns no pin.

- **The deviation on the pending-handoff shape is the right engineering call**
  and is explicitly tested, not silently dropped (see Important #4 for the
  process objection). That builder appends no blocker at all
  (`service.py:11382-11436`), so there is literally no row for a note to ride;
  `test_pending_handoff_shape_still_appends_no_advisor_blocker` pins that,
  with the reason in its docstring.

- **Provider text does not reach the surfaces the brief asked about.** I traced
  each: `note` is read in exactly two frontend files
  (`decisionPanelRows.ts:165`, `DecisionPanel.tsx:230-239`); it is rendered as a
  React text child inside `<p>`, with no `title`, no `aria-label`, no
  `dangerouslySetInnerHTML`, no URL; the Ask button's accessible name is built
  from `detail`, not `note` (pinned by a new test); the live region announces a
  count-derived string only (`DecisionPanel.tsx:115-143`); `parse_completion_gates`
  is the only reader of the envelope and `completion_gates_meta_value` /
  `completion_gates_meta_from_facts` (called from the single write site,
  `sessions/routes/_helpers.py:2771-2781`) the only writers; `audit_readiness`
  reads only `advisor_signoff_check_failed(result.checks)` and never the fact's
  text; and the shareable-review gate refuses to share a composition whose
  completion readiness is withheld, so a blocked advisor state — and therefore
  a note — cannot egress that way. **Verified clean.**

---

## Issues

### Critical (Must Fix)

None. Nothing here loses data, breaks the runtime, or lets provider text into a
surface it must not reach.

---

### Important (Should Fix)

#### I-1. The verdict token survives in the note for most real model formats

**File:** `src/elspeth/web/composer/service.py:11016` (with
`_ADVISOR_VERDICT_LINE_RE` at `:9492`)

`_advisor_note_text` strips the verdict lead with

```python
body = _ADVISOR_VERDICT_LINE_RE.sub("", findings_text.strip(), count=1)
```

`_ADVISOR_VERDICT_LINE_RE` is `^(CLEAN|FLAGGED)\s*(?:[:.\-–—]|$)` with
`re.IGNORECASE` **and no `re.MULTILINE`**. So `^` binds to position 0 and `$` to
end-of-string only. The token is removed only when the reply's very first
characters are `FLAGGED` followed, *on the same line*, by `:` / `.` / a dash —
or when `FLAGGED` is the entire reply.

The parser it feeds is deliberately far more tolerant. Its own docstring
(`service.py:9778-9784`) names the shapes it exists to accept because live
advisor models produce them: `**CLEAN**`, `Verdict: FLAGGED`, "a one-line
preamble before the verdict". Traced by hand against the regex:

| Advisor reply | Note the user sees |
|---|---|
| `FLAGGED: the sink drops rows.` | `the sink drops rows.` ✅ |
| `FLAGGED — sink omits the rating column.` | `sink omits the rating column.` ✅ |
| `FLAGGED\n\nThe sink drops rows.` | `FLAGGED\n\nThe sink drops rows.` ❌ |
| `**FLAGGED**: the sink drops rows.` | `**FLAGGED**: the sink drops rows.` ❌ |
| `Verdict: FLAGGED\nThe sink drops rows.` | `Verdict: FLAGGED\nThe sink drops rows.` ❌ |
| `Here is my assessment.\nFLAGGED: ...` | `Here is my assessment.\nFLAGGED: ...` ❌ |

(The third row is worth spelling out because it looks like it should work:
after `FLAGGED`, `\s*` consumes the newlines and then needs a terminator — the
next character is a letter, and `$` without `MULTILINE` will not match
mid-string, so the whole match fails and nothing is stripped.)

**Why it matters.** The entire ruling is "the reviewer's own words reach the
user, formatted for them". For three of the four formats the codebase
*documents as observed live*, the labelled "Reviewer's note" block opens with a
machine token — the exact protocol noise the header was invented to replace.
The failure is silent: no test covers a non-`FLAGGED:`-prefixed reply through
`_advisor_note_text`, and the two parser tests that do exercise the note
(`test_flagged_verdict_parses_category_steps_and_note`,
`test_missing_machine_lines_fall_back_to_other_and_no_steps`) both use the one
shape that works. The ledger flags only the `**FLAGGED**` sub-case, as
cosmetic; the preamble and `Verdict:` cases are unrecorded.

**Fix (sketch — read the caveat).** Mirror what the scanner already does rather
than re-deriving it: walk the lines, emphasis-strip each candidate, find the
first line the scanner would treat as the verdict, drop everything before it,
and remove the token (plus any `Verdict:` label and terminator) from that line.

```python
# Lead-only: label + token + terminator. Applied to ONE emphasis-stripped line.
_ADVISOR_NOTE_VERDICT_LEAD_RE = re.compile(
    r"^\s*(?:verdict\s*:\s*)?(?:CLEAN|FLAGGED)\s*(?:[:.\-–—]\s*|$)",
    re.IGNORECASE,
)

lines = findings_text.strip().splitlines()
body = findings_text.strip()
for index, raw_line in enumerate(lines):
    line = _ADVISOR_MARKDOWN_EMPHASIS_RE.sub("", raw_line).strip()
    if _ADVISOR_NOTE_VERDICT_LEAD_RE.match(line) is None:
        continue
    body = "\n".join([_ADVISOR_NOTE_VERDICT_LEAD_RE.sub("", line, count=1), *lines[index + 1 :]])
    break
```

**Caveat — do not implement this verbatim without checking the `Verdict:`
arm.** My first sketch reached for `_ADVISOR_CLEAN_VERDICT_LABEL_RE` for that
shape; that regex is `^(?i:verdict):\s*CLEAN\s*…` (`service.py:9519`) and matches
**CLEAN only**. There is no labelled FLAGGED arm — the parser accepts
`Verdict: FLAGGED` through `_ADVISOR_VERDICT_MARKER_RE` (`\b(CLEAN|FLAGGED)\b`
anywhere in the line, `:9484`) and `_ADVISOR_FLAGGED_ANYCASE_RE` (`:11146`).
The `(?:verdict\s*:\s*)?` group above is written to cover it, but the real fix
should factor the scanner's own accept decision (`service.py:9826-9831`) into a
shared helper so the note-stripper and the verdict-classifier cannot drift
apart — that drift is exactly what produced this bug.

Add one parametrised test over the six shapes in the table. Note that
`_ADVISOR_MARKDOWN_EMPHASIS_RE` must stay off the *body* (the ledger's reason —
it deletes underscores and would mangle step ids — is correct); applying it
only to the candidate verdict line keeps that property.

#### I-2. "Control characters stripped" does not include Unicode format characters

**File:** `src/elspeth/web/composer/service.py:11004`

```python
_ADVISOR_NOTE_CONTROL_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
```

This covers C0 (minus `\t\n\r`), DEL and ANSI CSI. It does **not** cover the
Unicode `Cf` format block — bidirectional overrides and isolates
(`U+202A`–`U+202E`, `U+2066`–`U+2069`), zero-width characters
(`U+200B`–`U+200D`, `U+FEFF`) or the line/paragraph separators `U+2028`/`U+2029`.
All of these survive into `note`, into the durable gate fact, and into the
`<p>` under `white-space: pre-wrap`.

**Concrete scenario.** A pipeline option field contains text with a `U+202E`
RIGHT-TO-LEFT OVERRIDE. The advisor reads that field as data, quotes it back in
its FLAGGED prose (which the prompt asks it to do — it is told to name the step
and the option), and the character lands in the note. In the panel the
remainder of the note renders reversed, so the rendered sentence differs from
the stored one. An operator who later reads the same note out of the session DB
sees different text than the user was shown. Zero-width characters let a word
be visually deleted from the rendered note while remaining in the stored
string. `U+2028` adds line breaks the character cap does not count as lines.

This is a hardening gap, not an exploit: there is no privilege boundary here,
the block is labelled "not verified by ELSPETH", and the attacker must route
through the advisor. But the plan's stated contract is "control characters and
the two fence sentinels stripped" (plan § Global Constraints), and `Cf` **are**
control characters by any reading a reviewer would apply. Because the strip
happens before truncation (correct ordering — see Verified clean), the fix is a
one-character-class change with no other consequence:

```python
r"\x1b\[[0-9;]*[A-Za-z]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​  ‪-‮⁦-⁩﻿]"
```

**Strip only the harmful subset.** Do *not* widen this to `​-‏` or
`⁠-⁤`, which is where I first landed: that range takes ZWNJ and ZWJ
(`U+200C`/`U+200D`) — mandatory orthography in Persian and Arabic, and the
joiner in every multi-codepoint emoji — and LRM/RLM (`U+200E`/`U+200F`), which
are legitimate directional *marks* an advisor writing about RTL data may need.
Removing them would mangle correct text. The characters that actually enable
the spoof are the embeddings and overrides (`U+202A`–`U+202E`), the isolates
(`U+2066`–`U+2069`), ZWSP (`U+200B`), BOM (`U+FEFF`) and the separators
(`U+2028`/`U+2029`).

Pin it with an RLO and a ZWSP in `test_note_is_bounded_and_sanitised`, and add a
negative control asserting a ZWNJ **survives** so the narrowing itself is
protected.

#### I-3. A containment pin silently stopped covering the surface it enumerates

**File:** `tests/unit/web/composer/test_advisor_checkpoint.py:3008-3036`
(unchanged by this branch)

`test_end_gate_final_flag_never_exposes_advisor_findings_on_human_surfaces`
asserts the canary is absent from a list of surfaces that includes
`runtime_preflight.model_dump_json()` (`:3026`) — i.e. the whole serialised
wire blob, blockers and all. After this branch that assertion is true **only
because the fixture builds `AdvisorCheckpointVerdict(...)` directly** (`:3018`),
so `note` takes its `None` default and the parser is never run. Drive the same
reply string through `_parse_advisor_checkpoint_guidance` and the canary is in
`model_dump_json()` by design.

The plan anticipated exactly this. Task 3 Step 4 lists the red and its rule:
"narrow the surface list: the canary MAY appear in `blockers[].note` of the
advisor row and nowhere else; name the ruling." The pin never went red, so the
executor never confronted it, and nothing was ledgered.

**Why it matters.** This is the repository's flagship R2-F13 pin. Its docstring
now reads "A final FLAG is internal evidence, never user-facing copy" — a
premise the 2026-09-22 ruling deliberately retired. A future reader will take
`model_dump_json()` in that list as proof the wire blob is clean of provider
prose. It is not, and the test does not say so.

**Fix.** Do what the plan said: replace the hand-built verdict with one from
`_parse_advisor_checkpoint_guidance(findings)`, exclude `blockers[].note` from
the enumerated surfaces with an inline comment naming the ruling, and add a
positive assertion that the note *is* present there. That converts the pin from
"passes because the fixture avoids the path" into an honest end-to-end proof of
the one-surface property — which is currently established only by composing
three separate tests (`test_note_is_bounded_and_sanitised` on the sanitiser,
`test_run_advisor_checkpoint_telemetry_failure_does_not_replace_completion:623`
on parser wiring, `test_blocked_result_carries_header_and_note` on the builder)
and never asserted in one place.

#### I-4. An unledgered deviation from the plan (correct, but unrecorded)

**Plan:** Task 3 Interfaces — "Each `_advisor_signoff_*_validation` builder
gains `category`, `step_ids`, `note` keyword-only parameters (REQUIRED, no
defaults)", naming four builders including
`_advisor_signoff_pending_handoff_validation`; and
`test_note_rides_every_preflight_shape` parametrised over
`["green", "red", "absent", "handoff"]` asserting `note == "x"` on all four.

**Implemented:** three builders gained the kwargs. The handoff builder
(`service.py:11382`) is untouched, its call site (`service.py:8634-8639`) passes
neither `category`, `step_ids` nor `note`, the parametrisation was renamed to
`test_note_rides_every_blocker_bearing_preflight_shape` with `handoff` dropped,
and a replacement test asserts the opposite property.

I judge the deviation **correct** — that arm appends no blocker by design
(elspeth-66717f0c99), so the plan's assertion could not have passed — and the
replacement test is better than the one it displaced. The objection is to the
record: the ledger has entries for far smaller judgements (the apostrophe
character, the `flex-wrap` reasoning) and none for this one. Per AGENTS.md
§ Claims Must Be Measured, a plan step that was not executed as written needs a
`Ruling:` line so the next reader does not have to diff plan against tree to
find it.

There is also a small **user-visible consequence** worth an explicit decision
rather than an implicit one: in the pending-handoff shape the user gets
"Completion advisory review did not clear" with no header and no reviewer's
words — precisely the opaque message this ruling exists to remove, just in a
narrower shape. It is arguably fine (the user must resolve the review card
first, and the next compose runs a fresh advisor pass that will land on a
blocker-bearing shape), but that reasoning belongs in the hand-back, not
inferred by a reviewer.

Two further deviations carry no `Ruling:` line:

- **The header/steps ordering was changed from what the plan specified.** The
  plan said "prefix `detail` with the header and, when `step_ids` is non-empty,
  *append* the step sentence"; the implementation puts both header sentences
  first, before the notice (`service.py:11284-11293`). The reasoning is sound
  and is written out in a code comment — a step list after "run again after
  your next pipeline change" would read as next-steps advice — but a code
  comment is not the ledger, and this is a deliberate departure from a written
  instruction.
- **Task 7 has no ledger entries at all.** The `t7-*.log` artefacts exist in
  the sdd directory (ruff, mypy, build, lints-after, the base/after
  position-stripped corpus files), but nothing in `progress.md` records what
  they showed. I have therefore made **no claim** anywhere in this review about
  gate outcomes, the trust-tier corpus comparison or the frontend build; where
  the brief stated those as given, I have treated them as the brief's
  assertions, not as verified facts.

#### I-5. A planned HTTP-level `/validate` assertion was silently not written

**Plan:** Task 4 Step 1, final paragraph — extend the seeded fact dict in
`tests/unit/web/sessions/test_routes.py` with `"note": "N"` and add one
assertion on the `/validate` response:
`response.json()["readiness"]["blockers"][0]["note"] == "N"`.

**Implemented:** `test_routes.py` changed in seven places
(`:11482, 11492, 11501, 12835, 12906, 12931, 12951`) and **every one of them
adds `note=None` / `"note": None` as a fixture repair**. There is no `"note":
"N"` anywhere in the file and no assertion on a `/validate` response body. The
step was skipped, and it is not ledgered.

**Why it matters, and why it is only Important.** The plan's own Review Focus
item 4 — "Reload after the block, then `/validate` on the unchanged graph…
Expected: the panel shows the same header and note as the blocking turn" — is
currently proven only at the unit seam
(`test_completion_gates.py:598`, parse → merge → blocker object). What the HTTP
test would add is that the route actually *serialises* `note` into the response
body the frontend decodes. The residual risk is genuinely low: `note` is a
required field on a `_StrictResponse` pydantic model, so it cannot be omitted
from `model_dump`. But "low residual risk" is a judgement the executor should
have recorded as a ruling, not an omission a reviewer discovers by diffing the
plan against the tree — especially for the one scenario the plan singled out as
a review focus.

---

### Minor (Nice to Have)

#### M-1. The cap counts characters, not rendered lines

`ADVISOR_NOTE_MAX_CHARS = 600` (`service.py:10994`) with `\n` deliberately
preserved and `white-space: pre-wrap` (`chat.css:2350`) means a 600-character
note can render as ~300 lines. `.decision-panel-reviewer-note-text` has no
`max-height` and no line clamp.

I checked before grading this: `.chat-panel-dock` is `overflow-y: auto` with
`min-height: 0` (`chat.css:1092-1101`), so the panel scrolls and nothing is
pushed permanently off-screen — hence Minor, not Important. The cost is that
the blocker's own "Ask the composer about this" button, which `flex-wrap: wrap`
places *after* the full-width note, sits at the bottom of a wall of blank
lines. Note the codebase has already litigated this exact class of problem for
the sibling surface: `workspace.css:499-505` caps `readiness.blockers[0].detail`
at `44ch` precisely because it is "an ARBITRARY-LENGTH backend string". A
`max-height` with scroll, or collapsing runs of 3+ newlines in
`_advisor_note_text`, would close it.

#### M-2. Write/read asymmetry on the empty string

`parse_completion_gates` rejects `note=""` (`completion_gates.py:294-295`), but
nothing on the write path enforces non-emptiness: `ValidationReadinessBlocker.note`
is a bare `str | None` (`schemas.py:262`) and `completion_gates_meta_value`
copies `blocked[0].note` through verbatim (`completion_gates.py:209`). A future
builder that passes `""` would write a row that every subsequent read rejects
with an uncaught `ValueError` — and `parse_completion_gates` is called
uncaught from `/validate` (`execution/routes.py:1003`), execute
(`execution/service.py:1272, 1800, 2794`), compose (`routes/composer/compose.py:135`),
messages (`routes/messages.py:216`), audit readiness and shareable reviews. That
is a bricked session row, not a degraded one.

Not reachable today (every writer passes `None` or `_advisor_note_text` output,
which is never `""`), and it copies the existing `detail` convention exactly, so
this is a note rather than a defect. If you want it closed, normalising `""` →
`None` in `completion_gates_meta_value` is the one-line version.

#### M-3. Removing the machine lines leaves blank lines in the note

`_ADVISOR_CATEGORY_LINE_RE` / `_ADVISOR_STEPS_LINE_RE` use `$` under
`re.MULTILINE`, so `.sub("")` removes the line's *content* and leaves its
newline (`service.py:11017-11018`). When the advisor writes prose *after* the
machine lines — which the prompt does not forbid; it says "end with two lines",
and models append postscripts — the note carries two blank lines mid-paragraph,
visible under `pre-wrap`. Trailing-position cases are saved by the final
`.strip()`. Cheap fix: match `\n?` as part of the line patterns, or collapse
`\n{3,}` → `\n\n` before the cap.

#### M-4. The panel's test-pinned contract block does not mention the note

`DecisionPanel.tsx:22-40` carries an explicit "Contract (test-pinned, …)" list
enumerating the panel's guaranteed surfaces. The reviewer's-note block is now
test-pinned (five new tests) and is the only place in the product where provider
prose renders in the chat, and it is not in that list. A bullet — "a blocker row
with a non-null `note` renders the reviewer's own words as a text child under a
'not verified by ELSPETH' label; never markdown, never in the Ask draft" — keeps
the header honest for the next reader.

#### M-5. One new test asserts a state that production cannot produce

`test_advisor_note_reaches_only_the_blocker_note_field`
(`tests/unit/web/execution/test_completion_gates.py:264`) parametrises
`flagged_unrepairable` and asserts `blocker.note == note`. But
`_advisor_signoff_blocked_wording:1263-1277` documents that `findings` on that
reason "is always the backend-authored pre-scan string (the user-message arm is
this reason's only producer)", so `_advisor_blocked_result:8606` always computes
`note = None` there. The assertion is a characterisation of the builder's
signature, not of a reachable state. Harmless, but it reads as coverage it is
not — worth a one-line docstring note, or dropping that parameter.

#### M-6. Interaction with the known false "withheld" sentence

Raised for context only — ledger line 16 already has this open with the
operator and the brief excludes it as a new finding. This branch makes it
**more** visible, not less: the new header is prefixed to the same notice
(`service.py:11291`), so the durable `detail` now reads

> The reviewer found the request not fully met. Steps named by the reviewer: merge_ab. Completion advisory review did not clear… ELSPETH withheld the composer's own summary of this exchange.

and the panel renders the reviewer's own summary in a labelled block directly
beneath it. Two adjacent sentences that contradict each other on one screen is
a worse reading than the pre-existing version. If the one-line default swap to
the `_PUBLISHED_` twin is going to happen, this branch is the natural place.

---

## The executor's rulings, judged

Every `Ruling:` line in `progress.md`, one verdict each.

| # | Ruling | Verdict |
|---|---|---|
| L3 | **Task 0 — base is a local integration** of `release/0.8.1` (`0bd0c5e40`) + plan 1 (`fdd90432f`) + plan 2 (`be825a79c`), one conflict resolved in `test_advisor_checkpoint.py`'s `drive_try_terminate` (kept plan 1's kwargs, dropped plan 2's retired flag) | **Right, and correctly costed.** The plan said to cut from the merged branch; plans 1 and 2 are unmerged by John's own instruction, so this was the only way to proceed without touching `release/0.8.1`. The honest consequence, which the ledger states: the reviewed range sits on a base that exists on no integration branch, so "this diff is clean" is conditional on plans 1 and 2 landing as-is. Flag it in the hand-back. |
| L6 | **Task 0 — baseline red set measured on `b87feeaea`**, one merge behind `1efadb8f1`; accepted without re-run because the delta is plan 2's composer-only fix pass, which has its own control run with an identical red set | **Defensible, not ideal.** This is an inference about a red set rather than a measurement of it, which AGENTS.md § Claims Must Be Measured exists to discourage. The cited control (the fix-pass web run, same 2 reds) is real evidence and the delta really is composer-only, so I would not reverse it — but a re-run was cheap relative to the cost of misattributing a red. |
| L7 | **Task 1 — the plan's `_ADVISOR_NOTE_CONTROL_RE` alternation order was wrong**; CSI moved before the C0 class | **Right, and the best ruling in the ledger.** `ESC` is a C0 byte, so the plan's order left `[31m` behind. Caught from a RED test, not from reading — and pinned by an exact-output assertion. |
| L8 | **Task 1 — actual name is `_ADVISOR_VERDICT_LINE_RE`, not `_ADVISOR_VERDICT_LEAD_RE`; markdown emphasis deliberately not stripped from the note (it would mangle step ids), so `**FLAGGED**` stays verbatim — "cosmetic; hand back"** | **Half right.** The name correction is right and the emphasis reasoning is right — `_ADVISOR_MARKDOWN_EMPHASIS_RE` deletes `_`, which would corrupt the very step ids the note names. But the ruling stopped at the one shape it happened to notice. Asking "which *other* accepted verdict shapes leak the token?" would have found the preamble and `Verdict:` cases too, and those are not cosmetic. This is I-1. |
| L11 | **Task 2 — 47 Python + 28 TS sites inserted by AST-anchored scripts**, controlled by before/after counts, a known-negative file, `ruff format` unchanged and a full diff review | **Right, and the controls were the correct ones.** A *missing* site fails loudly (required field), so the only silent failure mode is a wrong-target insert — which is what the known-negative and the diff review address. I re-scanned every `+` line mentioning `note` in the test diff and found no misplaced insertion. |
| L12 | **Task 2 — the TS signature over-matched the `DecisionRow` blocker shape at three sites; reverted, deferred to Task 6** | **Right.** The structural signature (object literals with `code`+`detail`+`suggestion`) genuinely cannot distinguish the wire type from the row type. Reverting kept the two tasks separable, and Task 6 then added `note` to `DecisionRow` deliberately rather than by accident. |
| L16 | **Task 3 — FINDING escalated, not fixed**: the durable `detail` is built from the WITHHELD twin notice, whose trailing sentence is false since plan 2 publishes the prose | **Right to escalate rather than fix.** It is plan 2's scope and it churns ~4 pins; fixing it here would have widened this branch. See M-6 for how this branch worsens the reading. |
| L20 | **Task 5 — three doc epoch-64 sentences are history and stay** | **Right; I verified it independently.** Each of the three names its own change (`from epoch 63 to 64`, a per-epoch history list, a dated handoff record). Re-measured at `c8305590c`: no current-state `64` survives anywhere. |
| L23 | **Task 6 — the plan's test signature (`blockers={[...]}`) does not match `DecisionPanel`'s real props (`rows: readonly DecisionRow[]`)**; tests written against the real API | **Right.** The plan was simply wrong about the component's interface; the tests would not have compiled. |
| L24 | **Task 6 — `note` is deliberately not part of `decisionId`** | **Right, and I checked the stated reason rather than taking it.** `decisionPanelRows.ts:160` builds the id from `[code, component_id, detail, suggestion]` — `detail` **is** in the id, so "detail already moves when the verdict does" holds: a changed category or step list changes the header, changes `detail`, changes the id. The "two blockers differing only by note" worry is moot anyway — `decisionId` appends an occurrence counter, so byte-identical rows still get distinct React keys. |
| L25 | **Task 6 — straight apostrophe, not `&rsquo;`; em dash stays** | **Right, and measured** (one curly apostrophe in component copy, 972 em dashes). House style, correctly derived from the tree rather than from taste. |
| L26 | **Task 6 — `.decision-panel-item--blocker` gains `flex-wrap: wrap`; a note-less blocker still has two children so nothing wraps** | **Right; verified against the CSS.** The rule is scoped to the blocker modifier class (`chat.css:2329`), and no other row type carries it. |

**Unledgered deviations** (each judged in the Issues section above): the
pending-handoff builder (I-4), the header/steps ordering change (I-4), the
skipped HTTP `/validate` assertion (I-5), and the whole of Task 7.

---

## Declined to judge

- **The `_ADVISOR_SIGNOFF_PENDING_NOTICE` / withheld-twin falsehood itself** —
  already open with the operator, ledgered, excluded by the brief. Its
  *interaction* with this branch is at M-6.
- **Plan 1's known limit I1** (a block first arising on an unmutated turn
  persists no fact) — explicit plan non-goal, separate ruling.
- **Structured `remediation_options` from the advisor** — explicit plan
  non-goal; the unstructured note is the decided answer.
- **The two pre-existing `test_freeform_planner_failure_translation` reds** —
  excluded by the brief as base-state.
- **The trust-tier corpus** — excluded by the brief. The brief *states* the
  position-stripped diff against base was EMPTY at the final commit; Task 7 is
  unledgered, so I am repeating that, not confirming it. Separately, `9e13ef830`'s
  explicit-fallback rewrite (`_advisor_flagged_header:10836`) is the right way
  to answer an R1 `dict.get` finding — it replaces a silent default with a
  written-out branch *and* pins it
  (`test_header_falls_back_to_other_for_a_category_outside_the_closed_set`).
- **Test *results*** — the gate produces that evidence separately; this verdict
  is a code reading and makes no pass/fail claim about any suite.
- **Whether guided-lane surfaces need the note** — guided mode is being removed;
  investing there is against standing direction.
- **The END prompt's category vocabulary** (is `prompt_defect` the right fifth
  bucket?) — a product-copy question for the operator, not a code defect.

---

## Verified clean

The brief's five focus items, and what I actually checked for each:

1. **Provider text containment.** `findings_text` reaches no user surface;
   `note` reaches exactly `ValidationReadinessBlocker.note`, its gate fact, and
   the one `<p>`. Traced every consumer of `note` in `src/` (two frontend files,
   two backend modules), every `parse_completion_gates` call site (9 actual
   calls, across `execution/routes.py`, `execution/service.py` ×3,
   `audit_readiness/service.py`, `shareable_reviews/service.py`,
   `sessions/routes/_helpers.py`, `routes/composer/compose.py`,
   `routes/messages.py`), the
   single envelope write site, the audit-readiness projection (reads checks
   only), the shareable-review gate (refuses to share a withheld composition),
   the live region (count-derived text only) and the Ask button's accessible
   name (built from `detail`; pinned by a new test). Adversarial reading of
   `_advisor_note_text` produced I-1 and I-2; everything else held. In
   particular: **truncation cannot split an escape sequence** — the control
   strip runs at `:11020` *before* the cap at `:11022`, so no partial `ESC`
   can survive, and the ordering is correct by construction, not by luck.
   Very long single tokens are handled by `overflow-wrap: anywhere`
   (`chat.css:2352`). The truncation cannot return an empty string
   (`body[0]` is non-whitespace after the earlier `.strip()`, so `.rstrip()`
   cannot empty it), so it cannot produce a value the Tier-1 parser rejects.

2. **`_validated_advisor_step_ids`.** Membership set is complete and correct
   (`sources` keys + `nodes[].id` + `outputs[].name`, matching
   `CompositionState`'s three component families). An attacker-chosen string
   can only appear in the header if it is *simultaneously* in the safe charset
   and a real id, so the header is unforgeable. De-duplication preserves the
   advisor's order. 500 named ids is bounded twice — by the `known` set, and by
   the fact that every one of them is then a real component the user authored;
   the resulting header is long but truthful, and it rides the same scrollable
   dock as M-1.

3. **The Tier-1 parser change.** Every writer emits `note`
   (`completion_gates_meta_value:209`, `completion_gates_meta_from_facts:235`),
   and those two are the only producers of the envelope — the single write site
   at `sessions/routes/_helpers.py:2771-2781` goes through
   `completion_gates_meta_value`, with the carry-forward branch reusing a
   previously-parsed `prior_completion_gates` (which therefore already carries
   the key). An epoch-64 row genuinely is unreadable: `parse_completion_gates`
   raises `ValueError` at `:292` and no call site catches it. The epoch bump is
   therefore justified, not ceremonial. Round-trip, null-round-trip,
   missing-key and bad-type cases are all pinned
   (`test_completion_gates.py:588-657`).

4. **Epoch bump completeness.** 8 code pins + 2 test names + 15 doc references,
   re-measured independently at `c8305590c`; the coupled equality check in
   `sessions/schema.py` moved; the operator-facing `PRAGMA user_version` probe
   and `website/get-started.html` both updated. Nothing in `scripts/` or
   `deploy/` pins the epoch. No current-state `64` survives.

5. **The frontend note render.** React text child inside `<p>`; no `title`, no
   `aria-label`, no `dangerouslySetInnerHTML`, no URL, no markdown renderer —
   pinned by `querySelector("strong")`/`("a")` assertions and a
   `JSON.stringify(mock.calls)` check on the Ask draft. The CSS change is
   correctly scoped: `flex-wrap: wrap` lands only on
   `.decision-panel-item--blocker` (`chat.css:2329`), and a blocker without a
   note still renders exactly two children, so no existing row's layout moves.
   The executor's reasoning here (ledger line 26) checks out.

Also verified: the `AdvisorCheckpointVerdict` dataclass gains no new leak
surface — `findings_text` was already a field, so any `repr` that would now
expose `note` already exposed the superset it is derived from, and the
telemetry test at `:623` pins the canary out of both sinks while asserting the
note *is* parsed.

---

## Recommendations

1. Fix **I-1** before this ships. It is ~10 lines plus a parametrised test, and
   without it the headline user-visible improvement degrades on three of the
   four reply formats the codebase says it actually sees.
2. Fix **I-2** in the same commit — a single character class, pinned with two
   characters in the existing test.
3. Re-seat **I-3** as the plan instructed. It is the only item here that leaves
   a *security* pin claiming more than it proves, and the plan already wrote
   the instruction the executor would have followed had the test gone red.
4. Write **I-5**'s HTTP assertion, or ledger the decision not to. It is two
   lines in a test that already exists and seeds the fact.
5. Add the missing `Ruling:` lines (**I-4**: the handoff builder, the
   header/steps ordering) and a Task 7 ledger entry recording what the `t7-*`
   logs actually showed. Right now the gate evidence exists as files with no
   verdict attached to them, which is the shape AGENTS.md § Test Verification
   Policy exists to prevent.
6. M-1 and M-3 are both one-liners in `_advisor_note_text` (collapse runs of
   3+ newlines before the cap) and would close together.
7. For the hand-back, state explicitly that the pending-handoff shape carries no
   reviewer's note, so the operator rules on it rather than discovering it.

---

## Assessment

**Ready to merge: With fixes.**

**Reasoning:** The architecture is sound and the containment property the ruling
depends on holds under adversarial reading — one bounded field, one render site,
a server-owned header, and step ids that are provably drawn from real state. The
epoch bump, the Tier-1 seam and the AST-driven field insertion are all
better-controlled than this class of change usually is. What is not ready is the
note's own text: for three of the four advisor reply formats the parser
documents as observed live, the user's "Reviewer's note" opens with a machine
token (I-1), and the sanitiser's stated "control characters stripped" contract
does not cover Unicode format characters (I-2). Both are small, local and
testable. I-3 through I-5 are record-keeping and test-integrity debts — one
security pin that quietly stopped covering the surface it names, and three plan
steps that were changed or skipped without a `Ruling:` line — and none of them
should outlive the branch.

**Scope of this verdict:** a code reading of `1efadb8f1..c8305590c` only. I ran
no suite and make no claim about any gate, the trust-tier corpus, or the
frontend build; Task 7 is unledgered, so those remain the brief's assertions
rather than verified facts.
