# APS publication review — the 46 staged GitHub issues

**Date:** 2026-09-23
**Scope:** every `.md` file in `docs/github-issues/issues/` (46 files, 2,773 lines)
**Question:** does anything in these files reflect badly on the Australian Public Service?
**Target:** `github.com/dta-au/elspeth`, public, issues not deletable by either available account

**Verdict: 34 PASS · 10 FIX · 2 HOLD.** No file leaks an infrastructure identifier, a
client name, a person, a credential or a home path. Register is clean: zero first-person
narration, zero apology, zero blame, zero agent attribution. The two HOLDs are one
security-category ruling and one file that is structurally a stale internal snapshot rather
than an issue. The ten FIXes are small and mechanical.

---

## 1. A measured fact that changes what a HOLD means

**All 46 files are already published.** They are tracked, committed, and present on
`origin/release/0.8.1` — a branch of a repository measured public, not assumed public.

```
$ gh repo view dta-au/elspeth --json visibility,isPrivate
{"isPrivate":false,"visibility":"PUBLIC"}

$ git ls-tree --name-only origin/release/0.8.1 docs/github-issues/issues/ | wc -l
46
$ git diff --stat origin/release/0.8.1 HEAD -- docs/github-issues/issues/
9 files changed, 10 insertions(+), 10 deletions(-)      # the dead-link repair only
```

So importing them creates no new disclosure of their *text*. What the import adds is
**amplification and permanence**: a GitHub issue is indexed, notified, linked, and — per the
brief — not deletable by either available account, where a file in a docs directory is none
of those things.

Two consequences run through this report:

1. A HOLD here means *do not amplify, and decide what to do about the tracked file* — not
   *do not disclose*. Withholding an import alone does not un-publish anything.
2. For the one file where the content itself is the concern, the remedy is an edit to the
   tracked file **and** withholding the import. Holding the import alone would be theatre.

I have written the HOLD entries so that this report adds no attack detail beyond what the
already-published file contains. This report is itself a tracked file in the same public
repository.

---

## 2. Method and instrument controls

Every negative claim below ("no home paths", "no first person") was made with an instrument
that was proved to fire on a planted positive first, per `AGENTS.md` § Claims Must Be Measured.

| Check | Instrument | Control |
|---|---|---|
| Credentials, hostnames, account ids, session UUIDs, agent/lane names, home paths | `check_issues.py` | exits 0; **0 BLOCK, 2 warn** |
| Home paths, ARNs, 12-digit accounts, URLs, `lane-*`, `codex-*`, UUIDs | `grep -noEi` over all files | planted `/home/bob`, `arn:aws:iam::123456789012`, `https://staging.internal.example`, `lane-01`, `codex-foo` — all five matched |
| First person (`I`, `we`, `my`, `our`, `us`) | `grep -nEw` | planted "I found a thing / We failed to check / This is my fault" — all three matched |
| US spellings | `grep -noEi` over a 30-token list | `behaviour` present in 10 files (positive control) |
| Shouting | `grep -noE "\b[A-Z]{4,}\b"` | 26 distinct tokens returned, all inspected |

My first first-person pattern was mis-anchored and returned zero hits for the wrong reason.
Re-run with `grep -nEw` after the control, it returned two, both benign (§5.9 and an `I/O`
false positive). That is why the control matters: a clean answer from a broken instrument is
indistinguishable from a clean answer from a working one.

**Also verified:** `import_issues.py` publishes `title` and `body` **verbatim** with no
footer, no slug and no "migrated from" line. The filenames — which carry internal step ids
(`a-5-`, `d-5-`) and one US spelling (`terminalization`) — never reach GitHub. That is a
clean result, not an assumption.

---

## 3. Verdict table

| # | File (short title) | Verdict | One-line reason |
|---|---|---|---|
| 1 | Batch-aware transforms raise bare TypeError | PASS | Exemplary. Long (1,074 words) but earned — 12 sites, four non-obvious test traps |
| 2 | User customisation and standing preferences | PASS | Clean epic; authority constraint stated as a property to demonstrate |
| 3 | Dangling-coalesce detection | PASS | `needs-triage`; carries an honest `## Note` withdrawing most of the original report |
| 4 | `AggregationSettings.on_error` is inert | PASS | Model issue. Grep control shown inline |
| 5 | Resolved credential as input to DAG node identity | PASS | Security-adjacent — see §6.1. Pre-empts the misreading itself |
| 6 | Base64 data URLs survive LLM redaction | PASS | Security-adjacent — see §6.2. Gate's `ONLY` warn is a source quotation |
| 7 | Plan-time package/image compatibility guard | PASS | Honest about the removed AWS account; see §8 |
| 8 | Collector calibration can bless the wrong policy | PASS | Explicitly bounds blast radius to the eval harness |
| 9 | Collector vocabulary parity gaps | PASS | `needs-triage`; withdraws two of three headline findings in its own text |
| 10 | Composer advisor sign-off gate traps unchanged turns | PASS | Clean; unresolved question flagged as such |
| 11 | Composer header chip shows configured model | **FIX** | Names a live model/provider string unnecessarily — §5.2 |
| 12 | Planner authors schema options validation rejects | **FIX** | "destroying every earlier session" — §5.10 |
| 13 | Composer tests leak async retry tasks | PASS | `needs-triage`; states the mechanism is not diagnosed |
| 14 | Composer tool layer default-fills "discard" | PASS | Strong. Quotes a source comment containing `INVENT` — acceptable as quotation |
| 15 | Composer `validate()` green for unbuildable config | **FIX** | One personified first-person clause — §5.9 |
| 16 | Live test and evaluation environment for 1.0 | **FIX** | "enclave access" — §5.4 |
| 17 | Kubernetes deployment support with kind | PASS | Clean scope statement; honest that the source was a scope note |
| 18 | Frontend fixture corpus has no fan-in node | PASS | Exemplary — explicitly withdraws a wrong earlier reading |
| 19 | Staging Playwright credential/origin/storage | **FIX** | "the runner's disk" is inaccurate and over-escalates — §5.3, §6.4 |
| 20 | Identity and workflow governance | **FIX** | "kept verbatim" points a public reader at internal-voice rows — §5.6 |
| 21 | Lineage retirement guard matches spelling of insert | PASS | Model issue; negative control named as the deliverable |
| 22 | LLM fanout admission has no cardinality contract | PASS | Security-adjacent — see §6.5 |
| 23 | Integer output validation accepts integral floats | PASS | Clean, small, well-scoped |
| 24 | Move long composer turns off the synchronous request | PASS | Long but earned; records an abandoned approach so it is not rebuilt |
| 25 | One unrestorable proposal row bricks a session | **FIX** | "the maintainer has ruled on" — `ruling` is banned by STYLE — §5.7 |
| 26 | Partial failed-aggregation terminalisation | PASS | Clean; British `-isation` in title |
| 27 | `pdf_rasterize` does not enforce `max_page_bytes` | PASS | Security-adjacent — see §6.6. Explicitly bounds the claim |
| 28 | Aggregate plugin contract budget overflows | PASS | Excellent measurement discipline |
| 29 | Plugin source-hash gate skips `transforms/aws` | PASS | Security-adjacent — see §6.7 |
| 30 | Recovery fork-group reconstruction is quadratic | PASS | Model issue |
| 31 | Pre-merge review findings for release/0.8.1 | **HOLD** | Stale snapshot; highest quotability, lowest contributor value — §4.2 |
| 32 | Repair Azure cost mapping / auth / LLM response | **FIX** | "Someone with the original context" is internal voice — §5.8 |
| 33 | Repair composer live-review follow-ons | PASS | Clean |
| 34 | Repair multi-query LLM contracts | PASS | Clean; uses `artefact` |
| 35 | Retire guided as a user-facing mode | PASS | Clean |
| 36 | Self-hosted runner directories accumulate | **FIX** | Host capacity and load telemetry — §5.1 |
| 37 | Sentinel custody projection fails open | **HOLD** | Fail-open audit-integrity defect, unfixed — §4.1 |
| 38 | Source output-naming options outside admission | PASS | Clean; one `artifact` (see §7 footnote) |
| 39 | SQLite identity store saves non-UTC grant expiry | PASS | Security-adjacent — see §6.3 |
| 40 | Stage-1 construction probes for profiled plugins | PASS | Exemplary hedging — "declared residue", "not confirmed breakage" |
| 41 | Staged validation vs a repair budget of two | PASS | Clean |
| 42 | State engine completion to 1.0 | **FIX** | Undefined "lanes"; "verbatim" invitation — §5.5 |
| 43 | Transform prevalidation discards policy error code | PASS | Model issue; "Status of evidence" section is a credit to the project |
| 44 | Untrusted-content admission does not propagate taint | PASS | Security-adjacent — see §6.8. Flagged for ruling, not held |
| 45 | Untyped `app.state` service access | PASS | Honest about two production 500s; frames as a gate to build, not a confession |
| 46 | Shipped examples with env vars never validated | PASS | Clean |

---

## 4. HOLD — two files

### 4.1 `sentinel-custody-projection-fails-open-when-live-source-op.md`

**Category.** Unfixed defect in which a security-relevant control **fails open** and emits an
affirmative false assurance, in a product whose stated purpose is a trustworthy audit trail.
The file names the preconditions with enough precision to reproduce, and states that the
codebase itself produces the triggering shape.

This is the one file in the set that meets the project's own test in
`docs/github-issues/README.md`:

> An unfixed vulnerability belongs in a **private GitHub security advisory**, where the
> developers who must fix it can see it and the world cannot, and becomes a public issue once
> it is fixed.

It is the only one of the eight security-adjacent files (§6) where I could not argue the
answer to *"what does an attacker do differently after reading this?"* is empty. Two others
are close calls and are flagged as such rather than held — §6.3 and §6.8 — and for those I
could construct the empty answer. The remaining five either describe an absent control an
attacker gains nothing from knowing about, or a defect requiring access the attacker would
have to hold already.

**The ruling required.** Three questions, in order:

1. Does this defect meet the README's own bar for a private advisory? My reading is yes. If
   the maintainer disagrees, record why — the discrimination is close and the reasoning is
   worth keeping.
2. **If yes, withholding the import is not sufficient.** The file is already on
   `origin/release/0.8.1` (§1). The remedy is to move the content to a private advisory and
   remove or redact the tracked file in the same change. Holding only the import leaves the
   content exactly where it is and creates a false sense that it was contained.
3. If the fix lands first, this becomes an ordinary public issue and should be published in
   full. The file is well written and should not be watered down.

**What I did not do.** I have not restated the trigger conditions, the affected function or
the bypass shape in this report. They are in the file; repeating them here would make this
review the disclosure the hold exists to prevent.

**Not a reason to hold:** the defect being unflattering. It is not. A project that finds its
own fail-open and describes it this precisely is a project working correctly.

### 4.2 `release-0-8-1-pre-merge-findings-code-review-suite-state-2.md`

**Category.** Not a security matter. This is the single most quotable file in the set against
review dimension 2, and its contributor value is close to zero by its own admission.

What a hostile reader would lift, verbatim:

- "A run labelled a final clean full-suite run was not clean, and nothing in the notification
  said otherwise."
- "…it will certify a red run as green again."
- "three checks that cannot fail by construction"
- "two live-code edits inside a sweep that was meant to touch documentation only"
- "release notes describing the wrong release"

Each is true, and each is the kind of self-correction a healthy project performs. The problem
is not honesty — it is that the file is a **dated snapshot of a branch under review**, not an
issue, and it says so:

- "every number in it needs re-measuring before it is relied on"
- "None of the numbers above are current."
- "The per-finding evidence lived in child tickets that are not part of this set" — so a new
  reader cannot act on clusters 1 or 3 at all

Publishing it as a system-of-record issue means a permanent, indexed public entry whose own
text tells the reader it cannot be relied on, whose four actionable items are buried under a
list of failure counts, and whose most quotable sentences are about process rather than code.
That combination is what makes it a HOLD rather than a FIX: no mechanical edit repairs
"this file is a snapshot".

**The ruling required.** Choose one:

- **(a) Publish as written**, accepting a permanent public record of a 2026-09-10 branch state.
- **(b) Re-scope to the one durable finding** — the wrapper that reported its own exit code
  instead of the test runner's — and publish that as a normal `type/bug`. This is my
  recommendation, **conditional on one check I could not settle**: which wrapper. It is the
  only item in the file that is (i) still true regardless of re-measurement, (ii) independent
  of the release, (iii) genuinely startable, and (iv) a real class of false-green that any
  project benefits from seeing fixed. Clusters 1–3 become a fresh measurement when someone
  runs the suite, per the file's own item 1.
- **(c) Drop it.** Defensible: nothing in it is actionable without a fresh run.

**The condition on (b).** The file does not name the wrapper, and the answer decides whether
(b) is available at all. `scripts/full-suite-gate.sh` is a tracked script with exactly this
shape — its own header records that "with `--detach` the parent exits 0 once the child is
launched; the child's code is the RESULT line in `summary.txt`" (lines 79-80), and its
comment block cites transcript measurements of `pytest … | tail` masking exit codes. If that
is the wrapper, (b) stands and describes a genuine public-script defect worth fixing in the
open. But the file's phrasing — "nothing in the notification said otherwise" — equally fits
an agent harness's background-task notification, which is agent tooling and excluded from
this migration by the operator decision recorded in `docs/github-issues/README.md:55-60`. If
it is the harness, (b) is unavailable and (c) is the answer. Settle this from the source
ticket before choosing; it is a two-minute check for someone with that context and I did not
have it.

Whichever is chosen, **do not soften the wrapper finding**. "Reported exit code 0; that was
the wrapper's own exit code, not the test runner's" is exactly the right level of detail and
is a credit to whoever caught it.

---

## 5. FIX — ten files, exact edits

Apply mechanically. Nothing here removes a technical claim; every edit is register,
accuracy, or infrastructure redaction. All twelve OLD blocks below were verified to match
their target files exactly (`grep -F`), so each is applicable as written.

> **This section reproduces the strings it recommends removing.** The brief required exact
> old/new text so the edits could be applied mechanically, and that is incompatible with
> redacting them here. But this report is itself a tracked file in the same public
> repository, so once these fixes are applied the OLD blocks in §5.1, §5.2 and the quotations
> in §7.1 need the same treatment — or this report should not stay in the public tree. That
> is a decision for the maintainer, not something I have assumed either way.

### 5.1 `self-hosted-runner-work-directories-accumulate-unpruned-pe.md` — host telemetry

Two passages publish operational detail about a government-owned build host: total filesystem
capacity, current utilisation, projected exhaustion, and instantaneous load. Neither is needed
to size or fix the defect; the `120 GB / 114 directories` measurement already does that. The
STYLE redaction table bans internal hostnames and deployment URLs as attack surface; host
capacity and saturation belong in the same family.

**Edit 1 — OLD** (end of the "Measured on disk" paragraph):

> There were 28 runs on 5 September alone. The filesystem at the time of measurement: 1.7 TB total, 1.2 TB used, 463 GB available, 72% full. At the observed run rate the remaining headroom is weeks rather than months.

**NEW:**

> There were 28 runs on 5 September alone. At the observed run rate the accumulation is bounded only by the runners' available disk, so the growth ends in a full filesystem rather than a steady state.

**Edit 2 — OLD** (the whole `## Impact beyond disk` paragraph):

> The same machines run the project's local test suites. Disk pressure and cache eviction on a host measured at load 41 during concurrent CI may be upstream of some of the intermittent CI timing failures seen on this project. That link is **not** claimed as measured — it is a reason to fix this rather than defer it.

**NEW:**

> These runners are shared with the project's other test workloads, so disk pressure and cache eviction affect more than CI. Whether that contributes to the intermittent CI timing failures seen on this project is **not** claimed as measured — it is a reason to fix this rather than defer it.

### 5.2 `composer-header-shows-the-currently-configured-model-for-h.md` — live model string

The measured block names the exact model and provider a staging deployment of an Australian
Government tool was calling. That fact carries none of the issue's argument — the argument is
that the chip shows configuration rather than per-session provenance — and the file's own
`## Repro` section already abstracts to "composer model A" / "B". Genericising the block makes
the file internally consistent and removes a line a reader could lift out of context.

**OLD:**

```
Measured on a staging deployment on 2026-09-13. One session composed on 2026-09-07 records
its authoring model in its interpretation events:

    model_identifier: openrouter/anthropic/claude-sonnet-5
    model_version:    anthropic/claude-sonnet-5
    provider:         openrouter
```

**NEW:**

```
Measured on a staging deployment on 2026-09-13. One session composed on 2026-09-07 records
its authoring model in its interpretation events — three fields, all stamped at composition
time:

    model_identifier: <provider>/<vendor>/<model-A>
    model_version:    <vendor>/<model-A>
    provider:         <provider>
```

### 5.3 `harden-staging-playwright-credential-origin-storage-modes.md` — inaccurate escalation

"The runner's disk" reads as a CI runner. **Measured: it is not.** The harness runs only from
`npm run test:e2e:staging` / `:smoke` (`src/elspeth/web/frontend/package.json:24-25`); no
workflow under `.github/workflows/` references `STAGING_` at all; and
`playwright.config.ts:130` states it runs "never in the default/CI run". The token lands on
the workstation of whoever runs it, not on shared CI infrastructure.

This edit both corrects the record and removes an unwarranted implication that a live bearer
token persists on shared build infrastructure. It does not soften the defect — the file keeps
every one of its three findings.

**OLD:**

> This harness runs against a deployed environment with a real account. The bearer token sits on the runner's disk at default permissions for the life of the run and afterwards, and the set of tests that touch a shared deployment changes whenever anyone adds a spec file — without that being a decision anyone made.

**NEW:**

> This harness runs against a deployed environment with a real account. It is invoked manually (`npm run test:e2e:staging`) and is not wired into any CI workflow, so the token is written to the disk of whichever machine runs it — at default permissions, for the life of the run and afterwards. The set of tests that touch a shared deployment changes whenever anyone adds a spec file, without that being a decision anyone made.

### 5.4 `d-5-provide-the-live-test-and-evaluation-environment-for-1.md` — "enclave"

In an Australian government context "enclave" reads as a classified or protected environment.
Nothing else in the set suggests that is meant, and the sentence works without it.

**OLD:**

> An Azure development environment, the subscription and enclave access required to reach it,

**NEW:**

> An Azure development environment, the subscription and network access required to reach it,

### 5.5 `state-engine-completion-to-1-0-see-docs-programmes-state-e.md` — jargon and invitation

Two edits.

**Edit 1 — "lanes" is undefined.** STYLE names "lane" as internal vocabulary to delete. Here
it appears to mean verification profiles that were never executed, but a reader cannot tell,
and the sentence is the file's central claim about why the programme is incomplete.

**OLD:**

> The published assessment records the verdict as not complete, with the largest gaps being lanes that have never been executed rather than known-broken code.

**NEW:**

> The published assessment records the verdict as not complete. The largest gaps are verification profiles that have never been executed, rather than code known to be broken.

**Edit 2 — the "verbatim" invitation.** See §7.1. "Captured verbatim" actively directs a
public reader to unedited internal tracker prose.

**OLD:**

> - `docs/programmes/state-engine-1.0/tracker-rows.json` — the seven issues this one replaces, captured verbatim so nothing was lost in the consolidation.

**NEW:**

> - `docs/programmes/state-engine-1.0/tracker-rows.json` — the seven work items this one replaces, retained so nothing was lost in the consolidation.

### 5.6 `identity-and-workflow-governance-see-docs-programmes-ident.md` — the same invitation

**OLD:**

> - `tracker-rows.json` — the original work items, kept verbatim.

**NEW:**

> - `tracker-rows.json` — the original work items, retained for traceability.

### 5.7 `one-unrestorable-composition-proposals-row-permanently-bri.md` — "ruled"

STYLE lists "ruling" among the internal vocabulary that means nothing outside the project.

**OLD:**

> path is a design decision first and should not be started until the maintainer has ruled on
> authorisation. Neither has been built.

**NEW:**

> path is a design decision first and should not be started until the authorisation question
> above has been decided. Neither has been built.

### 5.8 `repair-azure-composer-cost-mapping-authentication-admissio.md` — internal voice

"Someone with the original context" addresses a colleague who was there. STYLE's whole premise
is a reader who was not.

**OLD:**

> The source for this ticket records a work programme rather than a diagnosed mechanism. The
> authentication-admission half is named in the title but nowhere characterised: no symptom, no
> mechanism, no location. Someone with the original context should supply that or drop it from
> the title.

**NEW:**

> This issue records a work programme rather than a diagnosed mechanism. The
> authentication-admission half is named in the title but nowhere characterised: no symptom, no
> mechanism, no location. It should be characterised — symptom, mechanism and location — or
> removed from the title before anyone picks this up.

### 5.9 `composer-validate-returns-green-for-a-field-mapper-config.md` — first person

The only first-person clause in all 46 files. It personifies the validator, which is a
defensible rhetorical choice, but it is the one sentence a register check stops on and the
meaning survives without it.

**OLD:**

> A green result that cannot distinguish "your configuration is fine" from "your configuration cannot build and I could not tell" weakens that premise,

**NEW:**

> A green result that cannot distinguish "this configuration is fine" from "this configuration cannot build and the check could not tell" weakens that premise,

### 5.10 `composer-planner-authors-schema-configs-that-unchanged-val.md` — "destroying"

The fact — there is no baseline because the session store was recreated — must stay; it is why
the issue's first deliverable is a verdict rather than a patch. The verb is the problem: "the
roll to 0.8.1 … destroying every earlier session" is a quotable line about an auditability
product losing its records, and it overstates what happened (a development deployment's store
was recreated at an epoch change).

**OLD:**

> There is no baseline to compare against. The deployment ran the 0.8.0 build from
> 2 September 2026 until 14 September 2026, and the roll to 0.8.1 recreated its session
> store, destroying every earlier session.

**NEW:**

> There is no baseline to compare against. The deployment ran the 0.8.0 build from
> 2 September 2026 until 14 September 2026, and the roll to 0.8.1 recreated its session
> store, so no session from the earlier build survives for comparison.

---

## 6. The security dimension, file by file

The discriminator applied throughout: **what does an attacker do differently after reading
this that they could not do before?** Category (a) — a public description helps an attack — is
HOLD. Category (b) — a robustness defect in a security-related control — costs nothing to
publish.

One standing counterweight: the source code is in the same public repository, so every defect
below is discoverable by reading it. That reduces but does not eliminate the value of an
issue to an attacker, because an issue is a signpost. The project's own rule (README) treats
the signpost as the thing to withhold, and I have applied that rule as written.

| File | Category | Verdict |
|---|---|---|
| 6.1 Resolved credential in DAG node identity | (b) | PASS |
| 6.2 Base64 data URLs survive redaction | (b) | PASS |
| 6.3 SQLite non-UTC grant expiry | (b), borderline | PASS with note |
| 6.4 Staging Playwright credential handling | (b) | FIX (§5.3) |
| 6.5 LLM fanout admission cardinality | (b) | PASS |
| 6.6 `pdf_rasterize` worker page bytes | (b) | PASS |
| 6.7 Plugin source-hash gate skips `aws` | (b) | PASS |
| 6.8 Untrusted-content taint propagation | (b), borderline | PASS with note |
| 4.1 Sentinel custody projection fails open | **(a)** | **HOLD** |

**6.1 — Resolved credential as an input to DAG node identity.** The consequence described is
broken resume after credential rotation, not key recovery. The file pre-empts the misreading
itself: "At 48 bits over a large configuration this is not a practical secret-recovery vector
and is not claimed as one. The point is narrower: a credential should not be an input to a
durable identifier at all." That is the correct disclosure and the correct disclaimer in one
paragraph. Nothing to change. It is also the file most likely to be *misquoted* by a reader
who stops at the title — but the remedy for that is the paragraph that is already there.

**6.2 — Base64 image data URLs survive redaction.** A data-minimisation failure inside the
project's own audit store. Exploiting it requires access to that store, at which point the
attacker has the underlying data anyway. Publishing gives an attacker nothing. It is a
privacy/retention story a hostile reader could run with, and the honest description is the
project's best defence against that story, not its cause.

**6.3 — SQLite non-UTC grant expiry (borderline).** An authorisation grant outlives its
expiry by the administrator's UTC offset. This is a control failing open, which is why I
looked hard at it. It stays PASS because: the beneficiary is the grantee, who discovers it
passively by continuing to work; the window is bounded by the offset; it requires both a
SQLite store and an administrator submitting a non-UTC offset; and PostgreSQL — the profile a
production deployment would use — is unaffected and the file says so. An attacker learns
nothing actionable. **Worth a note to the maintainer:** if SQLite is a supported production
session store for any deployment target, this is worth reconsidering, because the file
publishes a precise recipe for a privilege that outlives its expiry.

**6.4 — Staging Playwright.** Demoted to (b) on measurement (§5.3): manually invoked, no CI
wiring, no `STAGING_` secret in any workflow. Apply the FIX and publish.

**6.5 — LLM fanout admission.** A spend-control gate that may be bypassable by hosting a model
on a node kind the guard's subject loop skips. The exposure is the deployment's own budget,
triggered by its own authenticated author. The file is careful to say this is the subject
model as written, not an observed escape. PASS.

**6.6 — `pdf_rasterize`.** The threat model is an already-compromised render worker. The file
explicitly narrows the claim — "this is not an arbitrary-file-read: the escape is in size and
content, not in location" — which is exactly the discipline that makes it safe to publish.

**6.7 — Plugin source-hash gate skips `transforms/aws`.** A CI integrity check does not cover
four transforms. Exploiting it requires commit access, at which point code review is the
control and the attacker could also edit the gate. Defence-in-depth coverage gap, not an
exploit. The file's framing ("an omission rather than a deliberate exclusion") is right.

**6.8 — Untrusted-content taint propagation (borderline).** This one I reconsidered. It
announces that a security control — the prompt shield — provably does not fire for
source-originated content, and names the affected sources. That reads like category (a). It
stays PASS because an attacker who can write to a blob store a pipeline reads will inject
regardless; knowing no shield fires changes nothing they *do*. It is an **absent** control
with an unsettled design (`type/task`, three open semantics questions), and a design decision
measured in months does not belong in a private advisory where nobody can work on it.
**Flag for the maintainer** as the closest call after §4.1: if the taint design is scheduled,
publishing is clearly right; if it is indefinite, a maintainer may prefer to defer.

---

## 7. Adjacent findings — outside the 46, but reached through them

### 7.1 Two issues direct public readers at unedited internal tracker prose

`state-engine-completion-to-1-0` and `identity-and-workflow-governance` each point at a
`tracker-rows.json` and describe it as holding the original rows "verbatim". Those files are
tracked and **already on `origin/release/0.8.1`** (commit `d6db534ff`), so they are public
today; the issues do not create the exposure, they advertise it.

Scanned with the controlled instrument from §2, the two programme folders contain **no**
home paths, ARNs, account ids, hostnames, session UUIDs, or agent/lane names. That is the good
news. What they do contain is internal register that would fail the project's own `STYLE.md`:

- `state-engine-1.0/tracker-rows.json` — `"STRUCTURAL FACT … 73/73 legs are 'unknown', ZERO
  confirmed"`, `"The verdict is SINGLE-GATED ON AWS"`, `"AWS was torn down 2026-08-10"`,
  `"the disputed Task 9 closure"`. ALL-CAPS emphasis, banned by STYLE; and the most quotable
  sentence in the repository sits in a file two issues recommend.
- `identity-workflow-governance/tracker-rows.json` — `"The operator chose …"` (a decision
  attributed to an unnamed individual, review dimension 4) and two first-person `"I "`
  occurrences.

**Recommendation.** Apply the §5.5 / §5.6 edits so the issues stop advertising the files as
verbatim source, and separately run `check_issues.py`'s rules over
`docs/programmes/**` and clean the two JSON files. That is a repo edit, not a publication
decision, and it is outside the scope I was given — flagging it, not doing it.

### 7.2 The two `held/` files

`docs/github-issues/held/` contains two issues correctly withheld because verification found
the work already present. `held/README.md` states the evidence and the decision needed for
each. I did not review them (out of scope) and note only that the mechanism is working as
designed — and that the reason recorded there is a good one: *"Publishing an issue that
asserts a defect a contributor would immediately find fixed … makes the whole set less
trustworthy."*

---

## 8. Cross-file narrative a hostile reader would assemble

Worth seeing whole, though it needs no per-file change. Three files independently tell a
reader that the project lost its cloud test environment mid-programme:

- `build-the-plan-time-…` — "an AWS account that was removed on 2026-08-10"; the shipped
  example pins a digest nobody can now verify
- `d-5-provide-the-live-test-…` — deployment evidence for 1.0 cannot be produced without an
  environment that does not exist
- `state-engine-1.0/tracker-rows.json` (already public, §7.1) — "The verdict is SINGLE-GATED
  ON AWS … AWS was torn down 2026-08-10"

Assembled, that reads as: a 1.0 completeness claim gated on verification the project cannot
currently perform. **The `d-5` file handles this exactly right** — it refuses to let the claim
rest on evidence never collected, and demands "one of two outcomes, not neither": run the
acceptance module live, or merge a change narrowing `DEPLOYMENT_STARTUP_PROFILES` and the
stated scope of the 1.0 claim. That is the strongest APS-facing sentence in the whole set, and
it is what turns the narrative from a liability into a demonstration of discipline.

No edit recommended. The maintainer should simply know the three files will be read together.

---

## 9. What passed, and why it is worth saying

Against the dimensions in the brief:

- **Named or identifiable people:** none. No reviewer names, no quoted decisions attributed to
  an individual, no "the operator ruled". "The maintainer" appears four times as a role in a
  decision-needed sense, which is the correct register for an open-source issue. (The one
  "ruled" instance is FIX §5.7.)
- **AI-agent authorship framing:** effectively absent. Two incidental matches — a model
  identifier in a measured record (FIX §5.2) and one "lanes" (FIX §5.5). Nothing attributes a
  defect to an agent's decision; nothing implies a government system was built without human
  review. Given how heavily this project uses agents, that is a deliberate and successful
  redaction.
- **Register:** zero first person (one personification, FIX §5.9), zero apology, zero blame,
  zero self-flagellation. Every "never tested / unexercised / inert" phrase I checked is
  attached to a specific mechanism and a specific fix, which is analysis rather than confession.
- **Shouting:** the 26 ALL-CAPS tokens are protocol names (`HTTP`, `JSON`, `POST`), enum
  members (`UNTRUSTED`, `UNROUTED`, `FAILURE`), measurement-table headers, or quotations from
  source comments. The gate's one `ONLY` warning is a quoted docstring. No editorial shouting.
- **Australian English:** consistent. `behaviour`, `serialise`, `normalise`, `authoring`,
  `terminalisation`, `catalogue`, `artefact` all present and correct. Footnote: three prose
  instances of `artifact` (`source-output-naming`, `repair-composer-live-review`) and one
  "proof catalog" (`state-engine`) sit beside `artefact`/`catalogue` elsewhere. Every other
  `catalog`/`artifacts` match is a real file path (`web/catalog/knob_schema.py`,
  `artifacts.py`) and must not be changed. Not worth a verdict; fix opportunistically.
- **Honesty:** the standout. Files 3, 9, 18, 40 and 43 each **withdraw or narrow their own
  founding claim** in the published text — "That reading was wrong and is withdrawn",
  "two of the three findings it ranked highest are not observable in the current tree",
  "This is a declared residue of a measured llm-specific defect, not confirmed breakage".
  Several distinguish measured from inferred in a dedicated section. A reader assessing
  whether this project is well run will find that more persuasive than any absence of defects.

**The honest summary for a hostile reader:** these 46 issues describe a pre-release system
whose maintainers measure their own defects precisely, publish the measurement alongside the
claim, and withdraw their own conclusions when the evidence does not hold. Nothing in them is
embarrassing to the Australian Public Service. Two need a decision before they go out, ten
need a small edit, and the rest are ready.
