# Web Composer in One Hour — Draft Training Plan

**Status:** DRAFT v1 (2026-08-30). Written against branch HEAD `39ce17e2c`.
**Format:** small-group session (4–8 people), one trainer, one hour, hands-on.
**Slides:** [`composer-training-one-hour-slides.html`](composer-training-one-hour-slides.html)
— a self-contained deck, one slide per numbered slide below, with these
speaker notes built in (open in a browser; `N` toggles notes, `P` prints one
slide per page).
**Purpose of this document:** a slide-by-slide, minute-by-minute plan that can
be turned directly into a deck and a spoken presentation. Every slide has
(a) the bullets that go on the slide, (b) speaker notes, and (c) where
applicable a demo script using the exact strings the UI shows.

---

## 0. Audience, outcomes, and pitch

**Audience.** Competent adults who have never used ELSPETH. Assume they know
what a CSV, a URL, a browser tab, and an LLM are. Do **not** assume they know
what a pipeline is, why anyone audits one, or what "validation" buys them.
The first principles taught here are ELSPETH's, not computing's.

**By the end of the hour every participant can:**

1. Explain a pipeline as *Sense → Decide → Act*, with *Audit* recording all three.
2. Build, validate, and run a pipeline in the Composer from a plain-English
   request, and read the graph, spec, and YAML it produced.
3. Recognise and act on every kind of card the Composer puts in front of them:
   a **proposal** (Accept / Reject) and a **decision the LLM made** (Acknowledge
   / Approve / Change…).
4. Say what the Audit panel's six rows mean and why some **block a run** and
   others are **advisory**.
5. Name the four advanced shapes (fork + coalesce, row union, scope + collector,
   batch aggregation) and know which one a given problem needs.
6. Get work *out*: Export YAML, Import YAML, Save for review, revert a version,
   fork a message, and switch the detail level.

**The pitch in one line (use it verbatim on slide 1 and slide 30):**

> "Validation and audit are part of the workflow, not after-the-fact
> diagnostics." — `README.md`

---

## 1. Trainer pre-flight (do this before the session, not during)

| # | Item | Why |
|---|------|-----|
| 1 | A deployment with a working composer LLM and a required-control implementation (prompt shield + content safety) authorised. | Segment 5's best teaching moment — the auto-wired controls — only fires if the deployment has them. Check `Audit:` shows `Ready` on a known-good session. |
| 2 | One trainer account plus one account per participant. Every fresh account starts on the **first-run tutorial**; decide (see §3, Segment 2 variant) whether participants do it as pre-work or skip it. | `App.tsx`: a new account lands on the tutorial, not the composer. |
| 3 | `examples/threshold_gate/input.csv` on the trainer machine and on each participant's machine (8 rows: id, name, amount, category). | The hands-on build in Segment 3. |
| 4 | **Pre-built sessions in the trainer account**, one per advanced shape (Segment 6): fork + coalesce with two LLM reviewers; row-union A/B; scope + collector over exploded pages; batch aggregation. Name them so they sort visibly in `Find a session…`. | Planner turns on advanced shapes take real time. Build once, load live. |
| 5 | One session deliberately left with a **validation error** (e.g. an LLM node whose required field is not produced upstream). | Segment 7 demo of the humanised error. |
| 6 | Trainer account: detail level on **Standard**; `Composer preferences → Reset tutorial` available if you intend to run the tutorial live. | Segment 8 flips the level live; the flip is the demo. |
| 7 | Project the browser at ≥ 960 px width. Below that the two panes collapse to a `Compose` / `Pipeline` switcher and the resize handle disappears. | The whole shell tour assumes the two-pane layout. |
| 8 | Have `?` (keyboard shortcuts) and `Ctrl+K` (command palette) ready as fall-backs when a button is hard to find on a projector. | |

**Timing reality check.** One hour is enough for one live build and one live
advanced load. It is not enough for participants to build an advanced shape
themselves. The plan is: participants build the simple pipeline (Segment 3)
and add an LLM to it (Segment 5); the trainer demonstrates the advanced shapes
(Segment 6) from pre-built sessions.

---

## 2. Minute-by-minute overview

| Min | Seg | Title | Mode | Slides |
|-----|-----|-------|------|--------|
| 0–5 | 1 | Why this exists: a pipeline, and why it must be auditable | Talk | 1–4 |
| 5–11 | 2 | The shell: what is on the screen | Trainer demo | 5–8 |
| 11–24 | 3 | Your first pipeline: threshold gate, from a sentence | **Hands-on** | 9–14 |
| 24–28 | 4 | What just happened: proposals, validation, the run | Talk over their screens | 15–17 |
| 28–38 | 5 | Adding an LLM: the decisions the LLM made, and the controls you did not ask for | **Hands-on** | 18–22 |
| 38–47 | 6 | Advanced shapes: fork/coalesce, row union, scope/collector, aggregation | Trainer demo (pre-built) | 23–27 |
| 47–53 | 7 | Trust and evidence: readiness rows, failures explained, what the run recorded | Trainer demo | 28–31 |
| 53–58 | 8 | Getting work out: YAML, review links, versions, forks, detail level | Trainer demo + quick try | 32–35 |
| 58–60 | 9 | Recap and where to go next | Talk | 36–37 |

---

## 3. Segments, slide by slide

Slide bullets are what goes on the slide. *Speaker notes* are what you say.
**Demo** blocks use the UI's exact strings in `code`.

---

### Segment 1 — Why this exists (0–5 min, slides 1–4)

**Slide 1 — Title.**
- ELSPETH Web Composer in one hour
- "Build and run auditable data pipelines." (the login-page tagline)

*Notes.* Say the tagline, then the one-line pitch. Tell them the plan: they will
build one pipeline themselves, add an LLM to it, and watch the harder shapes.

**Slide 2 — What a pipeline is.**
- Rows come in. Each row is processed the same way. Rows go out.
- **Sense** — "Read data in — files, APIs, or a sentence you type."
- **Decide** — "LLMs, rules, or gates rate, classify, or route each row."
- **Act** — "Write auditable outputs — every decision tied to its source."

*Notes.* This is the tutorial's own welcome copy (`components/tutorial/copy.ts`),
so it is the same wording participants will meet in the product. Give a
concrete example: 8 transactions come in; a rule sends the ones over $1000 to
one file and the rest to another. That *is* a pipeline. Everything in the hour
is a variation on it.

**Slide 3 — Why "auditable" is the whole point.**
- "Every output can be traced to its source with complete audit trail."
- If ELSPETH produced an output, you can ask "why?" and get: the source row,
  the transforms applied, the external calls made, the routing decisions, why
  it ended up where it did.
- "This is not optional. This is not best-effort. This is the reason ELSPETH
  exists." — `docs/release/guarantees.md`, The Core Promise

*Notes.* Contrast with the two things people already know: spreadsheets and
scripts (no record of *why*), and LLM workflow builders (easy to author, weak
on evidence — `README.md` "Why Elspeth Exists"). ELSPETH is for the cases where
an auditor, a regulator, or a colleague will ask "why did it do that?" and
silence is not an acceptable answer.

**Slide 4 — Two ways to write the same pipeline.**
- YAML, hand-edited and version-controlled — the operator path.
- The Web Composer — you describe it, an LLM builds it *through tools*, ELSPETH
  validates it. Same plugins, same validator, same runner, same audit trail.
- "The LLM proposes changes. ELSPETH records, validates, and gates the
  resulting pipeline." — `docs/release/composer-guide.md`

*Notes.* Plant the sentence you will return to all hour: **the LLM is not the
authority.** It proposes; you review; ELSPETH validates and records. The
Composer is not a second engine — "there is no separate UI-only validator"
(`docs/guides/user-manual.md`).

---

### Segment 2 — The shell (5–11 min, slides 5–8)

Trainer signs in and opens an empty new session. Narrate the screen top to
bottom. Participants watch; they get their own hands in Segment 3.

**Slide 5 — The screen (annotated screenshot).**
- Header: `ELSPETH` · `Session: …` switcher · `v1 ▾` version selector · `Account`
- Left: the **authoring pane** — the conversation (freeform) or the guided stepper
- Right: the **pipeline artifact** — tabs `Graph` · `Spec` · `YAML` · `Run`
- Bottom action bar: `Validation: …` · `Audit: …` · `Save for review` · `Import YAML` · `Run pipeline`
- The **Inspector** slides in when you click a status: tabs `Validation` · `Audit` (· `History` in guided)

*Notes.* Point, name, move on. The four artifact tabs become available as the
pipeline gains content (empty: only `Graph` and `Run`). The divider drags;
`Collapse authoring pane` gives the graph the whole width.

**Demo (2 min).**
1. `Account` → `Composer preferences`: show `Default mode for new sessions`
   (`Guided (recommended)` / `Freeform`), `Theme`, `Detail level` — leave all on defaults.
2. Press `?` — the `Keyboard shortcuts` dialog. Mention three: `Ctrl+K` command
   palette, `Ctrl+E` Run pipeline, `Ctrl+/` focus chat input.
3. `Plugin catalog` (toolbar, or `Ctrl+Shift+P`): tabs Sources / Transforms /
   Sinks. "These are the building blocks. You never have to remember their
   names — you describe what you want and the planner picks."

**Slide 6 — Guided and freeform: two conversations, one planner.**
- **Guided** walks four stages: `Source` → `Output` → `Transforms` → `Wire` (→ `Ready`)
- **Freeform** takes the whole request at once and refines
- "Guided and freeform differ in interaction, not in capability." — user manual
- Switch any time: `Switch to guided` / `Exit to freeform`

*Notes.* Both talk to the same planner and both can author every pipeline
structure. One asymmetry to state plainly: guided → freeform carries the graph
exactly; turning guided **on** over freeform work for the first time starts a
fresh wizard as a new version (the old draft stays in version history).

**Slide 7 — The three kinds of thing the planner shows you.**
- A **ribbon**: `Looked up: list_transforms` — it read something; nothing changed
- A **proposal card**: `Proposed: set_pipeline` … `Why:` … `Affects:` … `Accept` / `Reject`
- A **decision card**: "N decisions the LLM made — acknowledge each" — `Acknowledge` / `Approve` / `Change…`

*Notes.* This slide is the interaction model for the entire product. Nothing
executes, and nothing is committed to the pipeline, without you clicking one
of these. Say it now; they will see all three in the next ten minutes.

**Slide 8 — Variant: the first-run tutorial.**
- Every new account starts on it. Five steps: welcome → guided build → run → audit story → graduation.
- Fixed script: "Scrape these three synthetic project-brief pages and, for each
  page, have an LLM write a short summary of the page. Remove the raw HTML and
  write the rows to a json file."
- It is the *same* machinery as every real session — its only privilege is a frozen prompt (ADR-031).
- `Composer preferences → Reset tutorial` brings it back any time.

*Notes.* Two ways to use it. **(A) Pre-work:** ask participants to complete it
before the session (~10 min, needs the live LLM and network). Then Segment 2
can be shorter and you can reference "the assumption callout you saw". **(B)
Live opener:** run it yourself on a reset account in place of the shell demo —
it hits web scrape, LLM, auto-wired controls, run, and the audit story in one
pass. Do not do both; do not promise participants a re-run — a completed
tutorial wizard is not re-enterable within the same tutorial session.

---

### Segment 3 — Your first pipeline (11–24 min, slides 9–14) — HANDS-ON

Everyone builds the threshold-gate pipeline from `docs/guides/your-first-pipeline.md`
Option B. Trainer builds alongside on the projector, half a step ahead.

**Slide 9 — The task.**
- Input: 8 transactions (`id, name, amount, category`)
- Rule: amount > 1000 → `high_values.csv`; everything else → `normal.csv`
- Bonus outcome: afterwards, ask "why did Bob's $1500 go to high_values?" and get an answer.

**Slide 10 — Steps (leave this up while they work).**
1. Session switcher → `+ New session` (or `Ctrl+N`). Stay in guided.
2. In the chat input: `Upload file` → choose `input.csv`.
3. Paste this prompt:

   > Use the uploaded transactions CSV as the source. Build a pipeline that
   > routes rows with amount > 1000 to a high_values CSV output and all other
   > rows to a normal CSV output. Validate it before running.

4. Watch the stepper: `Source` → `Output` → `Transforms` → `Wire`.
5. Read each proposal. If asked for field types: `id: int`, `name: str`, `amount: int`, `category: str`.
6. At `Review wiring`, clear any pending acknowledgements, then confirm wiring.
7. When `Validation:` shows `Passed` → `Run pipeline` → confirm the disclosure dialog.

**Slide 11 — Reading a proposal card.**
- `Proposed: <tool>` — what it wants to do
- Summary — in your words; `Why:` — its reasoning; `Affects:` — which components
- Before/after diff, and `View arguments (JSON)` if you want the raw form
- `Accept` commits a new pipeline version. `Reject` discards and you can ask for a revision.

*Notes.* Circulate. The common stumbles: (1) someone types before uploading —
fine, guided retains the intent; (2) a "Source data / Data contract" card
appears — that is Segment 4's topic, tell them to read it and `Acknowledge`;
(3) the disclosure dialog on Run — read it out: "This run leaves the composer
and uses your stored credentials".

**Slide 12 — The graph.**
- `Graph` tab: one source, one gate, two sinks. Click a node → `<id> config`, `Settings`, `Connections & schema`.
- Node label ends with its state: "passed validation" / "has warnings" / "has validation errors" / "not yet validated"
- `Focus graph` (or `Ctrl+Shift+G`) for a full-screen view

**Slide 13 — The spec.**
- `Spec` tab: `Sources` / `Nodes` / `Outputs`, each with a one-sentence description
- Routing in plain English: "Reads from", "Then", "Routes", "Rows failing validation"
- "dropped (recorded in the audit trail)" — even a discard is evidence

*Notes.* The Spec tab is what a reviewer who does not read YAML reads. Point at
the `Routes` line on the gate: `'true' → high_values`, `'false' → output`.

**Slide 14 — The YAML.**
- `YAML` tab: the same pipeline as text. `Copy` / `Download` (`pipeline-v<n>.yaml`)
- Sections you will recognise: `sources:`, `gates:`, `sinks:`
- Note the banner: deployment-owned configuration (including the `landscape`
  audit destination) is supplied at run time and is never written here

*Notes.* Put the CLI example's `settings.yaml` side by side for 20 seconds —
it is the same shape. "Whatever you build here, an operator can run from the
command line, and vice versa."

---

### Segment 4 — What just happened (24–28 min, slides 15–17)

Short, spoken over their finished runs.

**Slide 15 — The four things ELSPETH did that you did not.**
- **Validated** the proposal before it became your pipeline (wiring, route targets, schema compatibility)
- **Recorded** a new version (`v2 ▾` in the header) — every accept is a version
- **Gated** the run: `Run pipeline` was disabled until `Validation: Passed`
- **Wrote the audit trail**: run configuration, every row's path, the gate result per row, output file hashes

**Slide 16 — The Run tab.**
- `Run results`: status, accounting (`rows read` / processed / rejected; tokens emitted / terminal / succeeded / failed; routed / quarantined / discarded)
- `Outputs`: every artefact with type, name, size, and a short content hash
- `Runs (n)` opens the history drawer

*Notes.* Ask them to find the count 4 / 4 across the two sinks. Then: "The
content hash is how you prove, later, that the file an auditor holds is the
file this run wrote."

**Slide 17 — Two vocabularies you now own.**
- Structure: **source**, **transform**, **gate**, **sink** (the UI says **Output**), **edge** / connection, **node**
- Discard vs quarantine: `discard` drops the row but records it; a production pipeline routes failures to a quarantine sink for review instead
- The word **fork** will mean two things later — a pipeline branch, and forking a chat message. Always say which.

---

### Segment 5 — Adding an LLM (28–38 min, slides 18–22) — HANDS-ON

Same session. Participants ask the planner to add a classification step.

**Slide 18 — The task.**
- Ask for: "Before routing, have an LLM classify each transaction's `category`
  as `retail`, `wholesale` or `corporate` from its name and amount, and write
  the label to a new field `llm_category`. Keep the same routing."
- Accept the proposal. Then **do not** run yet — look at the cards.

*Notes.* Freeform or guided both work; in guided, this is a revision of the
`Transforms` stage. Expect a longer planner turn — narrate the `Working on...`
indicator and its `Show details` while they wait.

**Slide 19 — "N decisions the LLM made — acknowledge each".**
- `… step · prompt` — "The LLM wrote the instruction for this step." → `View prompt`, then `Approve`
- `… step · model` — "The LLM picked `<model>`." → `Acknowledge` or `Change…`
- `… step · decision` — a judgement call it made (e.g. how it operationalised "classify")
- `Source data` / `Data contract` — what the pipeline *promises* about its input

*Notes.* This is the heart of the product. Every authored prompt is reviewed by
a human on a card — `Approve` is locked until you have opened `View prompt`
once. Read a prompt aloud. If the planner authored a threshold, scale, or
category boundary you did not specify, a `vague_term` decision card appears:
that is ELSPETH surfacing the judgement so you can `Change…` it. Quote the
tutorial: "The LLM made an assumption here … every assumption is surfaced in
the audit trail, and you can correct it by telling the composer what you meant."

**Slide 20 — The controls you did not ask for.**
- Look at the graph: a **prompt shield** before the LLM and a **content safety**
  check after it — nodes you did not author
- The deployment requires them on every path that carries LLM input/output.
  ELSPETH wired them and staged a decision card saying so
- "Anything fetched from outside is untrusted, and text on a page can be
  written to steer a model." — tutorial shield note

*Notes.* On a deployment where the controls are `required`, an LLM node
without them is *rejected*, not warned about; the Composer auto-wires the
deployment's chosen implementation and discloses it. If your deployment has no
shield authorised, the tutorial's caveat is the line to use: "Running an LLM
over fetched content without a shield is always a high-risk decision, not a
default." Either way this is the slide where "the LLM is not the authority"
becomes visible on screen.

**Slide 21 — What you can and cannot set on an LLM node.**
- You set: the prompt, the field the answer is written to, the `Model profile`
- The operator sets: provider, credentials, endpoint, concurrency, timeouts — via a **profile** you pick by name
- You never type an API key into a pipeline. `API keys & secrets` (chat `More actions`) stores them; the pipeline carries a *reference*

*Notes.* The `Composer: <model>` chip in the chat header is the model the
*planner* uses — it is display-only, set by the deployment. The model your
*pipeline* uses is a per-node option, reviewable on the `· model` card.

**Slide 22 — Run it.**
- Clear the cards → `Validation: Passed` → `Run pipeline`
- Run tab: LLM calls appear in the accounting; outputs now carry `llm_category`
- Every LLM call's request and response is recorded (hashed, payload stored) — that is what makes the run replayable

---

### Segment 6 — Advanced shapes (38–47 min, slides 23–27) — TRAINER DEMO

Load pre-built sessions from `Find a session…`. For each: show the `Graph`,
read the `Spec`'s routing lines, name the problem it solves. Two minutes each.

**Slide 23 — The nine structures the planner can author.**
- Linear chains · conditional gates · multiple outputs · **fork and coalesce** ·
  multi-source fan-in · **batch aggregation** · **row expansion** · error routing ·
  structured LLM output read downstream
- "These are the same nine canonical classes the parity corpus verifies across every authoring surface" — user manual

*Notes.* Do not enumerate on stage; leave the slide up. You are going to show four.

**Slide 24 — Fork + coalesce: two opinions, one row.**
- Problem: two independent LLM reviewers on the same document, merged back into one row
- Graph: gate with `Forks every row to` two branches → an LLM on each → a **coalesce** (`Merges branches`, `Merge policy: require_all`, `merge: nested`)
- Rule of thumb: a coalesce **merges fields**; the row count goes N → 1

*Notes.* Spec tab shows "every row continues to all branches". Mention the
merge policies in one breath — `require_all`, `best_effort` (with a timeout),
`first` — and move on.

**Slide 25 — Row union: A/B without merging.**
- Problem: compare prompt A vs prompt B across the same rows, then judge the *pair*
- Graph: fork → two LLM arms → **row union** → an aggregation that sees both rows
- A row union does **not** merge — it releases both rows, in order, as a correlated pair (N → N); the comparison step downstream does the judging
- Only end-of-source aggregation is allowed after a row union — a count trigger could split a pair

*Notes.* Contrast with slide 24 in one sentence: coalesce when you want one
combined row; row union when the rows must stay separate but travel together.

**Slide 26 — Scope + collector: one document, many pages.**
- Problem: explode a document into pages, process each, then reassemble *that document's* pages
- Graph: an expander (`json_explode` / `line_explode`) **opens a scope**; a **collector** closes it (`Scope`, `Scope opened by`)
- Policy is required, no default: `require_all` (any lost page fails the document) or `best_effort`
- A collector is not an aggregation: membership comes from the engine, not from a count or timer

**Slide 27 — Batch aggregation and the combined example.**
- Aggregation: statistics over a window — `trigger` by count, timeout, or end-of-source; `group_by`
- The finale shape (`examples/document_review_panel`): pages exploded → two reviewers forked *inside* each page → coalesce → collector per document → run-level aggregation
- Everything on this slide is authored by the same planner from a sentence — the vocabulary is the point, not the YAML

*Notes.* If time is short, drop slide 27's demo and just describe it. Close
with: "You will not remember the YAML. Remember the four words — fork,
coalesce, row union, collector — and describe the problem; the planner picks."

---

### Segment 7 — Trust and evidence (47–53 min, slides 28–31) — TRAINER DEMO

**Slide 28 — The Audit panel, before you run.**
- `Audit: Ready` → Inspector → `Audit`. Collapsed: `✓ Audit ready`. Expanded: six rows
- `Validation` · `Plugin trust` · `Provenance` · `Retention` · `LLM interpretations` · `Secrets`
- Each row is `Blocks run` or `Advisory`: "Rows marked 'Blocks run' must be clear before you can run this pipeline; the rest are advisory and do not stop a run."
- `Explain` → "What this pipeline will record" — the audit story in prose, *before* a single row moves

*Notes.* This is the thesis on screen: audit readiness is checked at authoring
time, not discovered afterwards. Open `Explain` and read two sentences of it.

**Slide 29 — When validation fails.**
- Load the broken session. `Validation: 1 errors` → Inspector → `Validation failed`
- Headline in plain English: "Two steps aren't connected correctly: the "X" step's output doesn't match what "Y" expects."
- `Suggestion:` underneath; `Technical details` for the raw dump; click the error → jumps to the node in `Graph`
- Fix it by telling the planner — it uses `explain_validation_error` (you see `Looked up: …`) and proposes a repair

*Notes.* Point out the two gates: `Run pipeline` shows its blocker as a plain
line above the button; `Save for review` is stricter still (an advisor
checkpoint can allow a run while still blocking completion). Advisory checks
never block Run.

**Slide 30 — Trust tiers: what happens when data is wrong.**
- **Tier 3 — anything from outside** (your CSV, an API, an LLM's reply): zero trust; validated at the boundary; a bad row is *quarantined*, the run continues
- **Tier 2 — your rows once a source has validated them**: types are trusted downstream; a wrong type there is an upstream bug to fix
- **Tier 1 — ELSPETH's own audit records**: fully trusted, so any anomaly *crashes* — "silently coercing bad audit data would be evidence tampering"
- "A CSV with garbage in row 500 should not crash a 10,000-row pipeline. A corrupted audit record should crash immediately." — README

*Notes.* One nuance worth saying: tiers follow the *data flow*, not the plugin.
An LLM in the middle of your graph creates a fresh Tier-3 boundary the moment
the model answers.

**Slide 31 — After the run: what survives, and for how long.**
- Run accounting closes the books: every row reached exactly one terminal outcome, or the run says so (`Audit closure`)
- Deeper questions live outside the browser: `elspeth explain --run latest --row 2` — the lineage tree for Bob's $1500
- Reproducibility grade: **FULL** (deterministic) · **REPLAY** (any LLM step — replay from recorded responses) · **ATTRIBUTABLE ONLY** (payloads purged; hashes survive)
- "Hashes survive payload deletion." — guarantees §1.4

*Notes.* Be honest about the boundary: the web UI shows accounting, artefacts
and fork/expand lineage frames; the full per-row lineage explorer is the CLI /
MCP surface. Every pipeline with an LLM is REPLAY-grade by construction.

---

### Segment 8 — Getting work out (53–58 min, slides 32–35)

Fast demo; invite participants to try any one thing on their own session.

**Slide 32 — YAML out, YAML in.**
- `YAML` tab → `Copy` / `Download` (`pipeline-v<n>.yaml`). Every export writes an audit event
- `Import YAML` (action bar): paste or choose a file; live `Parsed preview` + `Validation summary` before anything is sent; bind uploaded files for session-bound sources
- Import **replaces** the pipeline — the old one stays in version history; in guided it switches you to freeform

**Slide 33 — Save for review.**
- `Save for review` → `Share for review` → a `Share URL`
- The reviewer must be signed in; sees a frozen snapshot: graph, YAML, and the six-row audit panel *as it stood when you shared*; cannot edit, run, or fork
- Only available when validation passes and no readiness row is in error — "sharing a known-broken state is share-theatre"

**Slide 34 — Versions and forks.**
- `v<n> ▾` → `Composition history` → `Revert to v<n>` (confirm: "Revert pipeline")
- Hover any of *your* messages → pencil `Edit and fork from this message` → edit → `Fork`: a new session branching from that point, provenance recorded
- Sessions persist on their own — nothing to save; `Rename` / `Archive` from the switcher

**Slide 35 — Detail level.**
- `Account` → `Composer preferences` → `Detail level`: `Standard (recommended)` / `Show technical detail`
- Standard: a node shows its essentials; `Advanced settings (N)` collapsed; validation says "All N checks passed."
- Technical: `Advanced settings` pre-opened, `Raw options (JSON)` visible, every check itemised
- "The audit trail is always shown."

*Notes.* Flip it live on the LLM node from Segment 5. The same node, two
depths. Engineers and auditors flip it on; everyone else leaves it off.

---

### Segment 9 — Recap (58–60 min, slides 36–37)

**Slide 36 — What you can now do.**
- Describe a pipeline → review proposals → acknowledge the LLM's decisions → validate → run → read the evidence
- Four advanced words: **fork + coalesce**, **row union**, **scope + collector**, **aggregation**
- Six audit rows; `Blocks run` vs `Advisory`; three trust tiers
- Out: YAML, review link, versions, message fork, detail level

**Slide 37 — Where next.**
- `Help & documentation` (Account menu)
- `docs/guides/your-first-pipeline.md` — the walkthrough you just did, CLI and browser
- `docs/guides/user-manual.md` — Web Composer section; `docs/guides/troubleshooting.md` — "Web Composer — Guided Mode"
- `examples/README.md` — "If You Want to See…" lookup table; every advanced shape has a runnable example
- Pitch, one last time: "Validation and audit are part of the workflow, not after-the-fact diagnostics."

---

## 4. Vocabulary card (hand-out or final slide appendix)

| Say this | Not this | Why |
|----------|----------|-----|
| **Output** (guided stage), sink (structure) | — | The guided stepper shows `Output`; YAML and Spec say sink. Both are correct; introduce both. |
| **validation**, **audit readiness**, validation summary | preflight | "Preflight" never appears in Composer UI or user docs. |
| **proposal card**, **decision card** / approval card, assumption | "interpretation requirement", "staged", "surfaced" | The planner is instructed to describe these to users as cards to review. |
| **audit trail**, the **Audit panel** | Landscape | Landscape is the system's name for the store; say it once, then "audit trail". |
| **pipeline fork** vs **message fork** | "fork" alone | Two different things. |
| **coalesce** = merge fields (N → 1); **row union** = correlated pair, no merge (N → N) | "join" | The Spec tab says `Merges branches` for one and nothing of the kind for the other. |
| `Copy` / `Download` on the YAML tab | "the Export YAML button" | Export is no longer an action-bar button; the palette entry `Export YAML` and `Ctrl+Shift+Y` open the YAML tab. |
| `Blocks run` / `Advisory` | "error" / "warning" | The Audit panel's own badges. |
| **Model profile** | "model settings", "API key" | Users pick a profile by name; the operator owns credentials and endpoints. |

Two deliberate label splits exist and are pinned by a test — do not "fix" them
on slides: the command palette says `Execute pipeline` / `Export YAML` where the
shortcut sheet says `Run pipeline` / `Show YAML`.

---

## 5. Things to verify on the training deployment before delivering

These are true at HEAD `39ce17e2c` in the source tree; confirm they hold on the
deployment you will teach on.

1. **Required controls.** `deploy/aws-ecs/.../locals.tf` defaults both
   `prompt_shield` and `content_safety` to `required` with Bedrock
   implementations. Slide 20 assumes this. On a deployment with `recommend`
   or no implementation, use the override caveat wording instead.
2. **Authorised plugins.** A stock web deployment offers roughly nine
   transforms (`field_mapper`, `llm`, `web_scrape`, `line_explode`,
   `reference_join`, `report_assemble`, `passthrough`, the two Bedrock
   controls, plus Textract). Statistical `batch_*` transforms, `json_explode`,
   `value_transform`, `type_coerce`, `truncate` are **not** web-authorised by
   default — so Segment 6's pre-built sessions must be built with what your
   deployment allows (use `line_explode` as the scope opener, `report_assemble`
   as a collector, and LLM nodes as branch arms). Check `Plugin catalog`.
3. **Guided coverage.** The user manual at HEAD states guided authors all nine
   structures including require-all coalesce and cross-sink `on_write_failure`.
   An older snapshot listed those two as freeform-only. If your deployment is
   older than this branch, build the Segment 6 sessions in freeform.
4. **Planner turn time.** Time one advanced build on the deployment. If a turn
   exceeds ~90 s, do not build anything live beyond Segment 3.
5. **Tutorial pre-work.** If you choose Option A on slide 8, send the
   instruction at least a day ahead; the tutorial makes live LLM and network
   calls.

---

## 6. Sources consulted (for the slide author)

- `README.md` — pitch, Sense/Decide/Act, Data Trust Model, When to Use
- `docs/release/guarantees.md` — The Core Promise, §1.1–1.4
- `docs/release/composer-guide.md` — narrative framing, completion gestures
- `docs/guides/user-manual.md` §"Web Composer: Guided Mode" — stages, parity, nine structures, mode switching
- `docs/guides/your-first-pipeline.md` Option B — the Segment 3 lab, verbatim prompt
- `docs/guides/sharing-pipelines.md` — Save for review lifecycle
- `docs/guides/troubleshooting.md` §"Web Composer — Guided Mode"
- `docs/guides/landscape-mcp-analysis.md`, `docs/runbooks/investigate-routing.md` — Segment 7 "deeper questions"
- `docs/architecture/adr/031-tutorial-is-a-fixed-script-canary.md` — tutorial doctrine
- `docs/architecture/adr/040-composer-runtime-validation-posture.md` — validation surfaces
- `src/elspeth/web/frontend/src/components/tutorial/copy.ts`, `tutorialMachine.ts` — tutorial copy and frozen prompts
- `src/elspeth/web/frontend/src/components/` — `workspace/ArtifactWorkspace.tsx`, `workspace/WorkspaceActionBar.tsx`, `composer/CompletionBar.tsx`, `chat/AcknowledgementCard.tsx`, `chat/ToolCallCard.tsx`, `audit/AuditReadinessPanel.tsx`, `audit/ExplainDialog.tsx`, `settings/ComposerPreferencesPanel.tsx`, `inspector/OptionRows.tsx`, `common/ShortcutsHelp.tsx`, `sidebar/ImportYamlModal.tsx`, `chat/MessageBubble.tsx` — every UI string quoted above
- `src/elspeth/web/frontend/src/lib/validationHumaniser.ts` — the plain-English error headlines
- `src/elspeth/web/composer/required_controls.py`, `src/elspeth/web/plugin_policy/compiler.py`, `deploy/aws-ecs/terraform/modules/scenario/locals.tf` — required controls and the web plugin set
- `src/elspeth/web/composer/skills/pipeline_capabilities.md`, `pipeline_composer.md` — what the planner can author; the five review kinds
- `src/elspeth/core/landscape/reproducibility.py` — reproducibility grades
- `examples/README.md`, `examples/AGENTS.md`, `examples/{threshold_gate,fork_coalesce,row_union_ab_experiment,scope_collector,document_review_panel}/` — the shape ladder
