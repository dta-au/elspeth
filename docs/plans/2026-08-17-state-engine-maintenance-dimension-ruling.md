# State engine — `maintenance` dimension ruling (DRAFT for panel review)

Date: 2026-08-17
Owner issue: `elspeth-efb47cb5fd` (Tranche 1, T1-a). Parent: `elspeth-1040aa2143`.
Predecessor: `docs/plans/2026-08-17-state-engine-local-residual-split.md` §5, which
said the 133 first-party single-process `maintenance` cells "need a *pattern*, not
per-leg improvisation … Resolve before authoring 133 of them."

Status: **DRAFT — not ruled.** John rules; a small panel reviews this draft and the
candidate node first. Nothing here changes `completeness-criteria.md` yet.

## 1. The question

`completeness-criteria.md` line 81 defines the tenth mandatory dimension:

> `maintenance` — exact evidence locators remain collected and run in the
> maintained verification selection, with coherent actionable gap themes either
> live-owned in Filigree or explicitly unowned.

and line 135:

> Source inspection, architecture documents, decisions, plans, tracker state, and
> test names support mapping and interpretation. They cannot independently make
> a *behavioral* case pass. … `pytest` is the only behavioral promotion kind and
> `documentation` is support-only.

Every other dimension's acceptance text is about the leg's runtime behavior.
`maintenance` is about the *verification record* — whether the leg's evidence is
still selected and run, and whether its open gap has an owner. So: **is a
`maintenance` cell a behavioral case?** If yes, only an executed pytest node that
exercises the leg can promote it, and 127 of the 133 T1-a cells have no evident
shape. If no, what instrument promotes it, and what stops that instrument being
the "promotion by convenience" the review cycle demoted (RM `production_entry`)?

## 2. What the record already says (facts, with locators)

- **Precedent.** Exactly **two** `maintenance` cells have ever been promoted —
  F-10/leg-contract and F-12/leg-contract on the single-process profile
  (`proof-matrix.md` HG-10: "two promoted deliberate-absence cells") — backed by
  six mapped nodes (`coverage-mapping.json`: F-10 ×4, F-12 ×2). Their rationales
  say "source inspection, not executed behavior" / "not behavioral evidence" /
  "explicit reachability evidence for the deliberate absence", and were accepted
  under the forbidden family's text: "Refusal/reachability selectors and the
  deliberate-absence decision remain maintained." `review.md:32` of that
  assessment calls the F-10/F-12 maintenance claims sound. No non-F maintenance
  cell has ever been promoted.
- **Per-family acceptance** (`catalog.json` `family_dimension_acceptance`):
  transition "Exact executable selectors remain collected and the residual gap
  has coherent live ownership when actionable"; auxiliary "Exact selectors and
  any coherent actionable gap remain maintained"; run_coordination "Exact
  coordination selectors and actionable residual ownership remain maintained";
  production_boundary "Representative production selectors and coherent
  actionable ownership remain maintained"; read_model "The whole truth table and
  production consumer selectors remain maintained." All five are about the
  *selection*, none about runtime behavior.
- **Where per-leg locators live.** Local-lane cells in `evidence_selectors.json`
  carry **no** node ids (`test_local_lane_cells_must_not_carry_node_ids`); the
  lane carries a flat `node_ids` list (712 / 30 / 3). Per-`(leg, dimension,
  case, profile)` locators exist **only** inside a published assessment
  (`evidence[].coverage`, and the reviewed `coverage-mapping.json` beside it) (634 unique locators in 2026-08-15-0537). The package is
  digest-frozen and never edited.
- **What is already guarded continuously.**
  `tests/unit/architecture/test_state_engine_evidence_selectors.py::test_committed_selector_manifest_validates_against_the_v3_catalog`
  collects every lane's `node_ids` under the lane marker with a real,
  non-executing pytest collection and fails on any drift. Since bound locators ⊆
  lane node_ids at freeze, a rename of a bound test already turns CI red — and
  is repaired by editing the *manifest*, which leaves the **published record's
  locators silently stale**. `validate-package` (not in the CI job; see
  `test_state_engine_ci_selection.py`) checks only that selector *files* exist
  in current mode and declared test *names* only when the verdict is `complete`
  (`package.py:683-688`).
- **What is not guarded anywhere.** "Live-owned in Filigree" is checked by
  nothing: `tracker_snapshot` is exactly `{provider, captured_at, limitation}`
  (`package.py:533-541`), and `_validate_unresolved_metadata` only requires
  `owner_issue` be null or a non-empty string (`package.py:1411-1421`). Filigree
  is not in the repository. Today every unresolved leg names
  `elspeth-82592e3aa1` (71) or `elspeth-efb47cb5fd` (2); nothing would notice
  if either closed.

## 3. Options

### M1 — a pytest proof node per `(leg, case, profile)` (the candidate)

`tests/unit/architecture/test_state_engine_maintenance_dimension.py` (uncommitted
review artifact). 173 nodes = every catalog case whose `maintenance` cell is
required on `sqlite-wal-single-process-leader` (133 first-party residual + 38 provider
PB-09 + the 2 already promoted F-10/F-12 — the record-based subject needs no provider, so provider PB-09
maintenance cells become authorable without the contract fake). Each node reads
the maintained pointer (`proof-matrix.md` → current `assessment.json`) and asserts:
(a) the record's bound locators for that subject collect **exactly** under the
lane marker via `_collect_pytest_node_ids` (real subprocess collection, nothing
executed); (b) every unresolved cell of the subject, and the leg when
unresolved, names a Filigree issue id.

Measured: 173 passed, 36 s wall / 7 CPU-min (`-n 12`) — one subprocess collection
per node. Mutations run against it:

| # | Mutation | Result |
|---|---|---|
| A | rename a bound TS-03 test in the tree (reverted) | **FAILS** — collector: `not found: …::test_peer_claim_in_select_update_window_is_lock_excluded_single_owner` |
| B | lane marker no longer selects the locators | **FAILS** — collection drift |
| C | unresolved leg `owner_issue → null` | **FAILS** |
| D | unresolved cell `owner_issue → "not-an-issue"` | **FAILS** |
| E | owner is a **closed** Filigree issue (`elspeth-2ed41f0a4a`) | **PASSES** — cannot see the tracker |

Also cannot see: a `skip`/`xfail` mark added to a bound test (`--collect-only -q`
shows no marks; the lane's reporter refuses those *outcomes* at evidence time).

Structural problem, not a bug: because per-leg locators exist only in the frozen
record, M1(a) fails whenever the tree renames a bound test — correctly, "the exact
locators did not remain collected" — and stays failed until a **new assessment
re-maps** the cell. But the delta assessment's own evidence run executes the M1
node reading the *old* pointer, so the delta necessarily records that leg's
`maintenance` as `fail`. Every legitimate rename between assessments therefore
produces a maintenance failure the next assessment cannot clear. Escaping that
needs a maintained per-cell locator record outside frozen packages (i.e. local
lane cells carrying node_ids in the manifest — reversing an explicit design
decision) or a pre-publication pointer the node can read (breaks the closed
`safe_environment`).

### M2 — `maintenance` is assessment-derived, not asserted

Amend the criteria: `maintenance` is the one **non-behavioral** dimension. Its
subject is the assessment record, so its status is *derived by the assessment
tooling at freeze* rather than promoted by a test. `validate-package` derives
`(leg, case, profile).maintenance = pass` iff (i) every promoted cell of that
subject cites evidence whose collected nodes contain its locators (already
validated: "leg evidence does not cover its override"), and (ii) every unresolved
cell of the subject — and the leg when unresolved — either names an owner issue
that a **captured tracker snapshot** records as *open at capture time*, or is
`unowned` in a new closed vocabulary. Tooling change: `tracker_snapshot` gains
`owner_issues: [{id, status, captured_at}]` captured on the evidence host (where
Filigree lives), the validator requires every cited owner to appear open, the
derived cell status is emitted alongside overrides (overrides may not name
`maintenance` — a NEW validator restriction: `assessment.schema.json` line 162
leaves override `dimension` an unconstrained string today), the schema bumps. No pytest nodes; no rename deadlock (derived
from the record being frozen); liveness becomes machine-checked.

Cost: assessment-lib change (schema, capture, derive, validate) + criteria
amendment + a schema-3→4 note. It also means the maintenance status of every leg
is *the same kind of fact* — good, because it is.

### M2+G — M2 plus one whole-tree drift gate (recommended)

M2 for promotion, plus a single test in the maintained suite (not a proof node,
not a promotion instrument) that collects the **current published assessment's**
634 bound locators in **one** subprocess collection (4.7 s measured) and fails
naming every `(leg, dimension, case, profile)` whose locators drifted. This
restores the continuous, per-leg drift signal M1(a) gave — the part worth keeping
— at 1/90 the cost, without pretending it promotes anything: it says "the
published record is stale for these cells; the next assessment must re-map them",
which is exactly what a delta assessment's `changed_tuples` is for. Renames stay
CI-visible; the delta clears them; nothing deadlocks.

### M3 — leave `maintenance` as is

Only F-family cells ever get it (source pins under the forbidden family's text);
the other 5 families' maintenance cells stay `unknown` forever → verdict can never
be `complete` while the catalog marks them required. Not viable without a catalog
change making them N/A — which contradicts the criteria's "applicability is
catalog-owned … not merely because it is inconvenient".

## 4. Draft ruling text (for the criteria, if M2+G is chosen)

> ### `maintenance` is assessment-derived
>
> Ruled 2026-08-XX (elspeth-efb47cb5fd, T1-a). `maintenance` is the one
> dimension whose subject is the verification record rather than the leg's
> runtime behavior, so it is the one dimension no pytest node promotes. Its
> status is derived by `validate-package` at freeze from the assessment being
> published: `pass` when every promoted cell of the `(leg, case, profile)` cites
> evidence that collected and ran its exact locators in the lane's selection,
> and every unresolved cell of it — and the leg, when unresolved — names an
> owner issue the assessment's tracker snapshot recorded as open at capture, or
> is declared `unowned`; `fail` otherwise. Overrides may not name `maintenance`.
> The forbidden family keeps its additional executed pins (the deliberate-absence
> decision remains maintained). Between assessments, a whole-tree gate collects
> the current record's bound locators and fails on drift; that gate is a staleness
> signal for the next delta assessment, not evidence, and promotes nothing.

## 5. Questions the panel is asked to answer

1. Is the reading "`maintenance` is non-behavioral" faithful to lines 81 and 135
   and to the F-family precedent — or is it a convenient reading of the kind the
   review cycle rejected?
2. Does the M1 candidate discriminate (mutations A–D) enough to count as evidence
   at all, or is it a declaration test about a JSON file? Is the rename deadlock
   real, and is there a cheaper escape than the manifest change?
3. Is M2's derived status a real proof or "promotion by tooling"? What would make
   a derived `pass` *wrong* — and can the validator detect that?
4. Is `owner_issue: null` "explicitly unowned"? Should the vocabulary be closed?
5. Anything that lets an assessor turn 133 unknowns green without the gap being
   any better maintained.

## 6. Panel findings (2026-08-17) and revised recommendation

Three independent lenses (test-suite, reality, adversarial refuter). Every number
below was re-verified by the drafter against the tree.

**Verified facts that change the picture**

1. **56 of the 173 subjects have zero bound locators** (38 provider PB-09 + 18
   first-party: 8 PB-06 cases, 3 PB-10, AUX-03/04/05, F-01/02, …). For those the
   M1 node is only an owner-id regex, and M2's condition (i) is *vacuously* true.
   That is the R2-M3 tautology pattern verbatim. **Neither instrument may derive
   `pass` for a subject with no bound locators; those cells stay `unknown`.**
2. **Both instruments are content-blind.** A bound test gutted to `assert True`
   keeps its node id and passes M1(a) and any drift gate; the F-10 precedent
   (`test_state_engine_forbidden_paths.py:499-621`) AST-walks the retained body.
3. **The 634 bound locators are a strict subset of the 800 manifest node_ids.**
   So the existing manifest gate already turns red on every rename/deselection of
   a bound locator; M2+G's drift gate detects nothing new — it only attributes.
   After the manifest is repaired the drift gate alone stays red until a delta
   assessment re-maps: a red whose only repair is an assessment cycle **will be
   silenced**. Withdraw it as a CI gate; the attribution belongs in the manifest
   gate's failure text or in `validate-package` (extend `package.py:686`'s
   declared-name check to `current` mode).
4. **Ownership is two rows.** All 71 unresolved legs → `elspeth-82592e3aa1`; 71
   of 72 unresolved cells → **`elspeth-efb47cb5fd`, this ruling's own issue.**
   Existence+openness of two issues would satisfy 142 assertions; closing this
   issue flips 71 cells; the repair is to repoint at another umbrella. Line 81's
   "coherent actionable" is not checked by anything proposed.
5. **The precedent is two promoted cells (F-10, F-12), not six**, both under the
   forbidden family's deliberate-absence text, and their `pass` overrides carry no
   rationale (the "source inspection" wording lives only in mapping metadata).
6. **Line 135 names "tracker state" explicitly** among things that cannot
   independently promote. M2's derived pass is computed from tracker state + test
   names. Calling `maintenance` "non-behavioral" is therefore an **amendment** of
   the criteria (like Decisions 1 and 2), not a reading of them — and it must be
   ruled as one, or refused.
7. **PB-09 loophole.** Making the record the subject would let 72 provider/first-
   party PB-09 maintenance cells promote with no plugin exercised — routing round
   Decision 1 within a day of drawing it. Any ruling must say a record-subject
   dimension inherits no provider entitlement.
8. Override `dimension` is unconstrained (`assessment.schema.json:162`), so "no
   maintenance overrides" is a new validator restriction; `derived` is an
   unconstrained object, so a derived status is not schema-blocked.

**Revised recommendation (drafter's, advisory — John rules)**

- **Withdraw M1** (declaration test + rename deadlock recording a *false* `fail`)
  and **withdraw M2+G's gate** (redundant, will be silenced).
- **Do not promote maintenance by reinterpretation.** If `maintenance` is to be
  assessment-derived, rule it explicitly as an amendment to line 135 (add: "except
  `maintenance`, whose subject is the record and whose status is derived by
  `validate-package`") — and derive it **only** where all three predicates hold:
  (i) the subject has ≥1 promoted cell whose locators collected and passed in the
  cited lane run — zero-locator subjects stay `unknown`; (ii) every unresolved cell
  names an owner issue captured **open** in a new `tracker_snapshot.owner_issues`;
  (iii) coherence is enforced structurally: an owner issue may carry cells of at
  most one leg (or one leg family) — umbrella issues are refused by the validator
  — and `unowned` is a closed value only the review record may declare.
  Plus: no `maintenance` overrides; the forbidden family keeps its executed pins;
  a record-subject dimension inherits no provider entitlement (PB-09 provider
  cells still need Decision 1's fake or the live lane).
- **Otherwise M3**, honestly: the 133 (and 173) cells stay `unknown` under
  `insufficient_evidence`; "Completeness is binary" (line 11) already holds that
  state; unknown-forever is only fatal if `complete` is owed on a schedule.

Either way, `elspeth-efb47cb5fd` should stop being the owner of 71 cells: split
ownership per leg family before any tooling reads it, or the first thing a
liveness check proves is that this ruling's own issue is load-bearing.

**Not verified:** a cheaper deadlock escape than a maintained per-cell locator
record (the "stable proof marker" idea loosens "exact locators" and must be ruled
explicitly, not inherited).
