# Single-developer assumptions audit

**Date:** 2026-09-23
**Scope:** repository-wide sweep for assumptions that hold only while one person
works on ELSPETH, ahead of (a) graduating to multiple developers and (b)
migrating work tracking from Filigree onto GitHub Issues.
**Posture:** read-only. No file was edited and no test was run.

## Citation basis (read this before checking a line number)

- Measured HEAD: `74c0ce0db336e8ed67c6f532d070e7553fcd5059` (`git rev-parse HEAD`).
- Citations are against the **working tree** as read on 2026-09-23, not against
  HEAD, because `AGENTS.md` is locally modified.
- Measured delta: `git diff --stat HEAD -- AGENTS.md` → `1 file changed, 6
  insertions(+)`. The insertion is an uncommitted `# Operating Model` section at
  working-tree lines 17–22. **Every `AGENTS.md` citation below at line ≥ 17 is
  therefore 6 lines ahead of the same text in HEAD.** Verified by spot-check:
  working tree `AGENTS.md:284` ("maintained by a single developer") is HEAD
  line 278; working tree `:132` is HEAD line 126.
- All other files were clean at read time.
- **Every `file:line` citation below was mechanically re-verified** against the
  live files after drafting: each was extracted from this document, resolved with
  `sed -n "<line>p"`, and checked to contain the quoted text. One error class was
  caught and fixed that way — citations first taken from `sed -n 'A,Bp' | cat -n`
  output carry the *offset from A*, not the file line. If you add a citation,
  re-derive it with `grep -n` against the whole file.

### Instruments used, and their controls

| Claim | Instrument | Control |
|---|---|---|
| Branch protection state | `gh api repos/dta-au/elspeth/branches --jq '.[] \| {name, protected}'` | Positive control: `gh api repos/dta-au/elspeth` returned `{"full_name":"dta-au/elspeth","permissions":{"admin":false,...}}`, so the repo is readable and the **404 on `/branches/main/protection` is an `admin:false` permission limit, not "no protection configured"**. The per-branch `protected` boolean is readable without admin and is what is reported here. |
| Tracker-id census | `git grep -ohE "elspeth-[0-9a-f]{10}"` | Positive: must hit `AGENTS.md:132`, `:142`, `:345`, `:348` — it did. Negative: `echo "This line has no ticket id" \| grep -cE ...` → `0`. |
| CODEOWNERS absence | `find . -name CODEOWNERS` | Only three hits, all inside `node_modules` vendor trees; none at any GitHub-recognised path (`/`, `.github/`, `docs/`). |
| Tracked-vs-ignored config | `git ls-files`, `git check-ignore -v` | `.mcp.json` → ignored at `.gitignore:77`. `.claude/settings.json`, `.pre-commit-config.yaml`, `pyproject.toml` → tracked. |

**One limit is stated rather than measured:** the *detailed* protection rules on
`main` (required checks, required approvals, dismissal settings) could not be
read with this token. Finding **F-09** reports only the per-branch boolean, which
was readable. GOVERNANCE.md:73–75 already says these settings "require periodic
inspection before they can be claimed as enforced"; that inspection still needs
an admin token and is not performed here.

---

## Summary

**38 findings.** By theme:

| Theme | Findings | Must change | Should change | Deliberate — keep, but document |
|---|---|---|---|---|
| Custody & keys (F-01–F-06) | 6 | 2 | 2 | 2 |
| Review & merge (F-07–F-13) | 7 | 5 | 2 | 0 |
| Concurrency (F-14–F-19) | 6 | 0 | 2 | 4 |
| Onboarding (F-20–F-26) | 7 | 2 | 5 | 0 |
| Tracker (F-27–F-33, F-41) | 8 | 5 | 3 | 0 |
| Prose & framing (F-34–F-38) | 5 | 2 | 3 | 0 |
| Mode-dependent reasoning (F-39–F-40) | 2 | 0 | 2 | 0 |
| **Total** | **41** | **16** | **19** | **6** |

F-39 and F-40 were surfaced by the ADR-024 reference audit and are documented in
that subsection rather than under a theme. F-41 was surfaced by checking the
in-flight GitHub Issues migration and sits under Tracker.

Counts are derived from the finding headings, not hand-tallied:
`grep -oE "^(### |\*\*)F-[0-9]{2} .*" <this file> | grep -oE "MUST CHANGE|SHOULD CHANGE|DELIBERATE, KEEP" | sort | uniq -c`
→ `6 DELIBERATE, KEEP` / `16 MUST CHANGE` / `19 SHOULD CHANGE`; total heading count → `41`.

The single most serious finding is **F-01**: the judge-signature custody model's
safety depends on an unstated precondition — that only the key holder can push to
`main` and `release/**` — and that precondition is **measurably false today for
every `release/*` branch**, which is where 0.8.1 work actually lands.

The project has already written down that this transition is coming.
`ROADMAP.md:150` names package **F.1 Multi-developer readiness**, and
`ROADMAP.md:161` lists "Key custody and access to project records" as a decision
"Required by: Before shared-maintainer operation under F.1". This audit is the
itemised version of that row. Likewise `GOVERNANCE.md:77-82` pre-wrote the
step-up checklist for two-maintainer mode; the trigger it describes is now
firing, and none of the controls it names exist yet.

---

## The documented step-up: what it reaches, and what it does not

`GOVERNANCE.md` § Maintainer Continuity (`:48` onward) is the authority for this
transition, and this audit treats it as such. It defines two named modes and the
trigger between them (`GOVERNANCE.md:55-56`):

> "Single-maintainer mode holds until a second maintainer regularly participates
> in release-critical delivery."

**The headline result of mapping the audit onto that switch: the documented
step-up closes 4 of 41 findings, partially reaches 1, and never reaches the other
36.** The step-up is a well-specified *pull-request review* posture. It is not a
key-custody, onboarding, tracker, or documentation posture, and it was written
before the GitHub Issues migration existed. So "execute the step-up" is necessary
and nowhere near sufficient.

### The six named controls: current state, with evidence

`GOVERNANCE.md:77-82` names six controls. Two are repository artefacts this
project supplies; four are GitHub platform settings.

| # | Control (GOVERNANCE.md:77-82) | Kind | Current state | Evidence |
|---|---|---|---|---|
| 1 | "one required approving review" | Platform | **Unmeasured** | Needs admin; this token has `admin:false`. No repository obstacle found: there is **no auto-merge automation** to conflict with it (`grep -rn "auto-merge\|automerge\|gh pr merge" .github/` → no matches). |
| 2 | "stale-review dismissal on new commits" | Platform | **Unmeasured** | Needs admin. No repository dependency either way. |
| 3 | "last-push approval protection" | Platform | **Unmeasured** | Needs admin. No repository dependency either way. |
| 4 | "required conversation resolution" | Platform | **Unmeasured** | Needs admin. No repository dependency either way. |
| 5 | "CODEOWNERS or an equivalent ownership map for security-sensitive paths" | **Repository** | **ABSENT — measured** | `find . -name CODEOWNERS` → three hits, all in `node_modules` vendor trees; none at `/`, `.github/` or `docs/`. See **F-10**. |
| 6 | "review requirements for release tags or branches where the platform supports them" | **Repository + platform** | **ABSENT — measured** | `gh api repos/dta-au/elspeth/branches` → `main` `protected: true`; **every** `release/*` branch `protected: false`, including the live `release/0.8.1`. See **F-09**. |

**Controls 5 and 6 are the actionable ones today** — they are the two the project
can act on without admin access, and both are measurably absent.

**Controls 1–4 are unmeasured, not absent.** I state that as a limit rather than
a finding. `GOVERNANCE.md:73-75` already requires exactly this inspection:

> "Platform-configured
> controls such as branch protection require periodic inspection before they can
> be claimed as enforced."

That inspection needs a token with `admin` on `dta-au/elspeth`. The account used
here (`tachyon-beep`) has `{"admin":false,"maintain":false,"push":true}`. A second
account (`johnm-dta`) exists on this machine and may hold admin; I did **not**
test it, because reading another account's token was refused by the permission
system and working around that refusal would be wrong. **Someone with admin should
run `gh api repos/dta-au/elspeth/branches/main/protection` and record the output**
— that single command settles controls 1–4 and is the missing evidence in this
audit.

**One piece of good news, measured:** the aggregate required-status-check design
the step-up depends on already exists and is correct. `ci.yaml:1332-1333` defines
`ci-success` / "CI Success", and `enforce-allowlist-judge-gates.yaml:220-221`
defines `judge-gates-success` / "Judge gates success", with the rationale at
`enforce-allowlist-judge-gates.yaml:50-52` explaining why an aggregate is required
rather than the individual jobs. Turning on required review does not require
rebuilding the check topology.

### The gap the step-up does not close: it is PR-shaped, the worst exposure is push-shaped

**Every one of the six controls governs pull requests.** F-01 is not a
pull-request exposure. The HMAC key is injected on `github.event_name !=
'pull_request'` — that is, on **push** — and a direct push to an unprotected
`release/*` branch never passes through review at all. Required approving review,
stale dismissal, last-push protection and conversation resolution are all
inapplicable to it.

Control 6 is the only one that touches release branches, and its wording —
"review *requirements* for release tags or branches" — is satisfiable by a review
rule that still permits direct pushes. **F-01 is closed only if control 6 is
implemented as full branch protection that blocks direct pushes, plus the
environment gating in F-01's recommendation.** Implementing the step-up literally,
as written, would leave the most serious finding in this audit open. That is the
single most important thing this mapping surfaces.

### Coverage map: all 41 findings against the step-up

| Disposition | Count | Findings |
|---|---|---|
| **COVERED** — the documented step-up closes it | 4 | F-07, F-08, F-09, F-10 |
| **PARTIAL** — a named control touches it but does not close it | 1 | F-01 (control 6, only if implemented as push-blocking protection) |
| **GAP** — the switch does not reach it | 36 | F-02–F-06, F-11–F-38, F-39, F-40, F-41 |

Gaps by theme, and why the step-up misses each:

- **Custody & keys (F-02–F-06, 5 gaps).** The step-up says nothing about key
  custody, the signing role, succession, or the Codex account a second signer
  needs. `ROADMAP.md:161` — "Key custody and access to project records … Before
  shared-maintainer operation under F.1" — is the project's own acknowledgment
  that this is a separate decision from the review posture.
- **Tracker (F-27–F-33, 7 gaps).** GOVERNANCE.md predates the GitHub Issues
  migration; `GOVERNANCE.md:30` still says only "the project issue tracker". None
  of the six controls touches the tracker, the 301 dangling ids, or the tracked
  skills that drive agents to Filigree.
- **Onboarding (F-20–F-26, 7 gaps).** Platform review settings do not fix a
  tracked `SessionStart` hook that fails on a fresh clone (F-20), an undefined
  security channel (F-24), or a `CONTRIBUTING.md` with no PR section (F-11).
  Enabling required review while F-11 remains unwritten means a new contributor
  faces a review requirement that no document explains.
- **Concurrency (F-14–F-19, 6 gaps).** These are per-host rules about several
  agents sharing one machine. Correct as-is; out of the step-up's scope by nature.
- **Prose & framing (F-34–F-38, 5 gaps).** F-34 and F-35 are the textual
  consequences of F-07: once approvals are non-zero, `AGENTS.md:284`
  ("maintained by a single developer") and the three "ask the developer"
  escalations are stale. The step-up changes the platform, not the covenant.

**Sequencing consequence.** Do not flip the mode before F-11 and F-34/F-35, or
the repository will enforce a review requirement that its own contributor
documentation contradicts.

### ADR-024 reference audit

ADR-024 is retired (2026-09-13) and is **not** reported as live single-developer
debt — F-38 handles it as history. Checking every tracked file that still cites
it, as requested:

**All redirects are already correct.** The three sibling ADRs named in ADR-024's
own note have each been updated to mark it Retired and point at GOVERNANCE.md:

- `docs/architecture/adr/025-multi-source-ingestion.md:544-545` — "**Retired 2026-09-13**; the assurance posture moved to `GOVERNANCE.md` § Maintainer Continuity."
- `docs/architecture/adr/026-durable-token-scheduler.md:1069-1077` — same, and it additionally corrects a historical mis-attribution: "earlier revisions mis-attributed them to ADR-024, which never stated them. Retiring ADR-024 therefore changes nothing in this decision."
- `docs/architecture/adr/029-journal-is-barrier-buffer-truth.md:446-447` — same redirect.
- `docs/elspeth-lints/rationale.md:25-27` and `docs/architecture/adr/README.md:46` and `ARCHITECTURE.md:1037` — all already describe it as retired with the correct pointer.

No file treats ADR-024 as a live authority. **This part of the tree is clean and
needs no action.**

**But two files carry live reasoning that depends on single-maintainer mode being
current**, which the retirement did not touch and which the mode switch will
falsify. These are new findings surfaced by this check:

**F-39 — ADR-025 justifies a control by the absence of an independent reviewer. SHOULD CHANGE**

`docs/architecture/adr/025-multi-source-ingestion.md:545-548`:

> "The point stands under
> that posture: with no independent reviewer available, a recorded
> ADR is the control for a structural change of this size, and this"

**Why it breaks.** The premise "no independent reviewer available" becomes false
at the mode switch. The conclusion — that a recorded ADR substitutes for review —
then rests on a condition that no longer holds. The ADR should still be recorded;
the *reason given* needs restating.

**Recommendation.** Reword to say an ADR is required for a structural change of
this size on its own merits, independent of reviewer availability. Do this when
GOVERNANCE.md is rewritten, in the same change.

**F-40 — The analyzer's rationale binds its own evidentiary value to single-maintainer mode. SHOULD CHANGE**

`docs/elspeth-lints/rationale.md:20-24`:

> "For the repository governance posture that makes these analyzer results part of
> single-maintainer delivery evidence, read
> [GOVERNANCE.md](../../GOVERNANCE.md) § Maintainer Continuity. It records why
> ELSPETH currently uses automated gates instead of non-meaningful
> self-approval, and how the project steps up to two-person review when a second
> maintainer is assigned."

**Why it breaks.** This is unusually well-written — it already names the step-up
and will not become *wrong*. But "single-maintainer delivery evidence" is the
framing for why the custom analyzer exists at all (`ADR-023`), and after the
switch the honest statement is that the analyzer complements two-person review
rather than substituting for absent review. Left unedited, it undersells the tool
and dates the rationale.

**Recommendation.** Change "single-maintainer delivery evidence" to "delivery
evidence" and keep the GOVERNANCE.md pointer, which will then describe the new
mode automatically.

---

## Theme A — Custody & keys (6)

### F-01 — CI hands the operator-only HMAC key to anyone who can push to an unprotected `release/**` branch. MUST CHANGE

`.github/workflows/ci.yaml:455`:

> `          ELSPETH_JUDGE_METADATA_HMAC_KEY: ${{ github.event_name != 'pull_request' && secrets.ELSPETH_JUDGE_METADATA_HMAC_KEY || '' }}`

The same expression appears at `ci.yaml:472` and `ci.yaml:619`. The workflow
triggers on push to `release/**` (`ci.yaml:14-15`):

> ```
> on:
>   push:
>     branches: [main, master, "RC*", "release/**"]
> ```

and those push events run on the maintainer's own hardware (`ci.yaml:60`):

> `    runs-on: ${{ fromJSON((github.event_name != 'pull_request') && '["self-hosted","Linux","X64","nyx-ci","trusted"]' || '"ubuntu-24.04"') }}`

Measured branch protection:

```
{"name":"main","protected":true}
{"name":"release/0.8.0","protected":false}
{"name":"release/0.8.1","protected":false}
```

(every `release/*` branch returned `protected: false`).

**Why it breaks.** A workflow file runs from the ref that was pushed. Anyone with
push access can therefore push an edited `.github/workflows/ci.yaml` to
`release/0.8.1`, and that edited file executes on the `nyx-ci trusted`
self-hosted runner with `secrets.ELSPETH_JUDGE_METADATA_HMAC_KEY` in the
environment. That is arbitrary code execution on the maintainer's machine plus
exfiltration of the key whose entire threat model is stated at
`docs/judge-signature-handoff.md:10-13`:

> "The judge-metadata signature is an **HMAC** — a symmetric MAC. Any holder of
> `ELSPETH_JUDGE_METADATA_HMAC_KEY` can forge a signature: hand-write
> `judge_verdict: ACCEPTED` with a fabricated rationale over a publicly-computable
> fingerprint, sign it, and pass every gate."

Today the only thing standing between that text and reality is that one person
holds push access. The moment a second developer is added, the [O1] custody rule
becomes advisory.

Note the mitigation that *is* documented is narrower than it reads.
`docs/judge-signature-handoff.md:14-18` says:

> "The CI-exposure corollary is mitigated in `.github/workflows/ci.yaml`: every
> step that injects `ELSPETH_JUDGE_METADATA_HMAC_KEY` gates it on
> `github.event_name != 'pull_request'`, so PR-controlled code never runs with the
> secret present."

That is accurate and remains true. It addresses *PR*-controlled code. It does not
address *push*-controlled code, which is the gap here.
`.github/workflows/enforce-allowlist-judge-gates.yaml:41` states the intent more
broadly than the configuration delivers:

> `# The judge-metadata HMAC key is operator-custody and must never be reachable`
> `# from PR-controlled code`

**Recommendation.** Do all three, in this order:
1. Enable branch protection on `release/**` (a wildcard ruleset), matching `main`.
   This is the smallest change that restores the precondition.
2. Move the key out of `secrets` at the repository scope into a **GitHub
   Environment** with required reviewers, so the key-bearing job cannot run on a
   push without an approval step. This survives a future protection misconfiguration.
3. Reconsider whether `nyx-ci trusted` should run any job that a non-custodian can
   influence. Ephemeral runners for the key-bearing steps remove the persistent-host
   risk entirely.

### F-02 — Symmetric HMAC makes a signature unattributable the instant a second person holds the key. DELIBERATE, KEEP — but document the role and succession

`docs/judge-signature-handoff.md:20-22`:

> "- **An agent never holds the key.** Agents may *propose* work — survey the tree,
>   stage a bundle, run a non-authoritative preview judge — but the authoritative
>   verdict for a finding is only ever minted inside the operator-keyed step."

**Assessment: the single-custodian posture is correct security design and should
survive the transition.** Restricting a forgery-capable key to one holder is not
a single-developer artefact; it is the right answer at ten developers too.

**But two properties do not survive.** First, an HMAC signature identifies the
*key*, not the *person* — so a second custodian makes every signature
unattributable, and the audit trail silently loses the property it was built to
provide. Second, one custodian with no named backup is a bus-factor-of-one on
release capability: today nobody but the maintainer can sign, and nothing
documents what happens if they are unavailable.

The project already ships the better pattern elsewhere.
`docs/runbooks/aws-ecs-deployment.md:608-611`:

> "`ELSPETH_ACCEPTANCE_APPROVAL_KEYRING` names a mode-0600 protected JSON document
> with schema `elspeth.aws-ecs-approval-keyring.v1` and a non-empty `keys` map of
> approved key IDs to raw 32-byte Ed25519 public keys encoded as unpadded
> base64url."

That is an asymmetric, multi-signer, per-key-id model with fail-closed
verification — exactly the shape the judge signature would need. The HMAC seam is
the outlier in ELSPETH's own design vocabulary.

**Recommendation.** Keep single custody; make it a **named role** rather than a
person. Specifically: (a) define "signing custodian" in `GOVERNANCE.md` with a
named primary and one named backup; (b) document the handover procedure — the
`rekey` verb already exists (`AGENTS.md:371`), so handover means rotate, not
share; (c) record the tradeoff for moving judge metadata to the Ed25519 keyring
model, which would let a second signer sign *as themselves*. Do not silently add a
second HMAC holder — that is the one option that loses attribution without
anyone noticing.

### F-03 — The release gate contains exactly one human and no second-person path. MUST CHANGE

`AGENTS.md:366`:

> "defects or drift. The operator signs once, at package completion, after churn
> has settled."

and `AGENTS.md:167-168`:

> "Never hand-edit signatures: agents leave or stage
> key-free work and the operator signs when the package or release is complete."

**Why it breaks.** "The operator" is a definite singular with no role definition,
no backup, and no documented escalation. A contributor who completes a package
cannot finish it; the work parks until one specific person acts. With one
developer that is a scheduling detail. With several it is a standing bottleneck
and an unowned queue — and there is no documented answer to "the operator is on
leave and the release is blocked."

**Recommendation.** Replace "the operator" with the named role from F-02
throughout `AGENTS.md`, `CONTRIBUTING.md` and the skills, and add a short
"signing service level" note: who to ask, expected turnaround, and who acts when
the primary is unavailable.

### F-04 — The judge authenticates from one machine's installed CLI account. SHOULD CHANGE

`docs/judge-signature-handoff.md:55-56`:

> "The Codex subprocess authenticates from the installed CLI account state, not
> from a provider key passed by the signing shell."

`docs/maintainer/toolchain.md:283-286` confirms this is the normal signing path:

> "The
> maintainer's convention on top of it: judging — including the final signature
> verdict — runs on the Codex CLI harness with read-only tool access
> (`--judge-transport codex-cli --judge-tools readonly`)"

**Why it breaks.** A second custodian cannot sign without their own Codex CLI
account, logged in on their own machine. Nothing states this as a prerequisite, so
it will be discovered at the worst moment — during a release, by someone who
believed they had everything they needed. It also means signature provenance
depends on a per-machine account state that is invisible to the repository.

**Recommendation.** Add a "what a signing custodian needs" checklist to
`docs/judge-signature-handoff.md`: the HMAC key, a Codex CLI account, the
transport flags, and how the account is provisioned. Reference it from the
`judge-signature-workflow` skill.

### F-05 — Agents are told to verify key absence from their own shell, with no organisational control behind it. DELIBERATE, KEEP

`scripts/branch-safety-check.sh:252-253`:

> ```
> if [ -n "${ELSPETH_JUDGE_METADATA_HMAC_KEY:-}" ]; then
>     report FAIL hmac-key "ELSPETH_JUDGE_METADATA_HMAC_KEY is set in this shell ([O1]); tell the operator, do not run gates from here"
> ```

**Assessment: keep.** This is a genuine, well-built control and it scales — a
per-shell check works the same for ten developers as for one. It is listed here
only because its failure message says "tell the operator", inheriting the
singular from F-03, and because it is the *only* enforcement of [O1] outside CI:
there is no organisational control preventing a developer from having the key in
their environment, only a script that notices afterwards.

**Recommendation.** Keep the check unchanged; fix the message's "the operator" to
the named role. Consider promoting it into the pre-commit hook set so it runs
without being invoked deliberately.

### F-06 — The tracked MCP example still provisions the maintainer's private tooling. SHOULD CHANGE

`.mcp.json.example:24-25` (tracked):

> ```
>     "filigree": { "type": "stdio", "command": "filigree-mcp", "args": [] },
>     "loomweave": { "type": "stdio", "command": "loomweave", "args": ["serve"] }
> ```

**Why it breaks.** `ADR-043` (title, line 1) is explicit that these are
"the Maintainer's Integrations; the Project Ships No Others", and
`docs/maintainer/toolchain.md:3-5` says the toolchain "is not a requirement of
the project". The tracked example contradicts both by presenting them as part of
the standard setup — and one of them is the tracker being retired.

**Good news, measured:** the live `.mcp.json` is **not** tracked
(`git check-ignore -v .mcp.json` → `.gitignore:77`). This matters, because the
live file contains a hardcoded absolute interpreter path under the maintainer's
home directory, and a bearer token on line 38. (The literal path is deliberately
not reproduced here: AGENTS.md forbids a user-home path in a tracked file, and
`scripts/branch-safety-check.sh` enforces it on every commit.) The ignore rule is
doing real work; leave it.

**Recommendation.** Split `.mcp.json.example` into the first-party servers
(`elspeth-composer`, `elspeth-judge`, `elspeth-landscape` — these are product) and
drop the two maintainer tools, or move them to a clearly-labelled optional block.

---

## Theme B — Review & merge (7)

### F-07 — Zero required approvals is the documented posture, and its own retirement trigger is now firing. MUST CHANGE

`GOVERNANCE.md:61-63`:

> "- the required human approval count is zero while no independent maintainer is
>   routinely available; self-review cannot satisfy an approval requirement;"

`GOVERNANCE.md:55-59` states the condition:

> "Single-maintainer mode holds until a second maintainer regularly participates
> in release-critical delivery. A single-maintainer repository cannot honestly
> claim independent two-person review, and self-approval would add ceremony
> without improving safety, so the posture is deliberate rather than an
> accidental waiver of review discipline:"

**Why it breaks.** The reasoning was sound when written — self-approval genuinely
is ceremony. It stops being sound the moment a second developer exists, and the
document says so itself. This is the root finding of the whole theme: every other
review gap below is downstream of "approvals are zero".

**Recommendation.** Execute the step-up the document already specifies
(`GOVERNANCE.md:77-82`, quoted in F-08) and rewrite § Maintainer Continuity to
describe the new mode. Treat this as the flag-day change that the other review
findings hang off.

### F-08 — The two-maintainer checklist is pre-written and entirely unimplemented. MUST CHANGE

`GOVERNANCE.md:77-82`:

> "When a second independent maintainer regularly participates in
> release-critical delivery, ELSPETH enters **two-maintainer mode** and enables
> one required approving review, stale-review dismissal on new commits,
> last-push approval protection, required conversation resolution, CODEOWNERS or
> an equivalent ownership map for security-sensitive paths, and review
> requirements for release tags or branches where the platform supports them."

**Why it breaks.** It does not break — it is unusually good foresight. It is a
finding because it is a checklist with no owner, no target date, and no tracking
item, and because *none* of its six controls exists today (CODEOWNERS measured
absent, `release/*` measured unprotected). A checklist nobody owns is not a plan.

**Recommendation.** Make the six items trackable work with a named owner and
target, as the first milestone of ROADMAP F.1, then change the prose from
future-conditional to a record of what was enabled and when.

**Where** to track them is not mine to assume, and there is a live tension — see
**F-41**. The operator ruling recorded at `docs/github-issues/README.md:55-60`
keeps "internal governance" work out of GitHub, and these six controls are
plausibly that category. Raise the placement question rather than filing them
somewhere by default.

### F-09 — Every `release/*` branch is unprotected; `main` is protected. MUST CHANGE

Measured:

```
{"name":"main","protected":true}
{"name":"release/0.8.0","protected":false}
{"name":"release/0.8.1","protected":false}
{"name":"release/0.7.1","protected":false}
{"name":"release/0.7.0","protected":false}
{"name":"release/0.6.1","protected":false}
{"name":"release/0.6.0","protected":false}
```

**Why it breaks.** `release/0.8.1` is the current working branch and the target of
active delivery. It receives the full CI treatment including the key injection in
F-01, and it is the branch a second developer would push to. Protecting `main`
while leaving the branch that actually receives work unprotected inverts the
intended control.

This also undercuts a documented CI assumption.
`.github/workflows/enforce-allowlist-judge-gates.yaml:50-52`:

> `# BRANCH PROTECTION: the aggregate ``judge-gates-success`` job at the bottom`
> `# is the single required status check for these gates (mirroring ci.yaml's`
> `# ``ci-success``). Branch protection MUST require ``Judge gates success`` from`

That "MUST" cannot be satisfied on a branch with no protection at all.

**Recommendation.** Add a `release/**` ruleset requiring the same checks as
`main`. Verify with an admin token and record the output, since GOVERNANCE.md:73-75
requires exactly that inspection before protection may be claimed as enforced.

### F-10 — No CODEOWNERS anywhere in the repository. MUST CHANGE

Measured: `find . -name CODEOWNERS` returns three paths, all under vendored
`node_modules/@braintree/sanitize-url/.github/CODEOWNERS`. None at `/`,
`.github/`, or `docs/`.

**Why it breaks.** With one developer, ownership is implicit. With several, the
security-sensitive paths this project cares about — `src/elspeth/web/sessions`,
`src/elspeth/web/composer`, `config/cicd/enforce_tier_model/`, `.github/workflows/`
— have no required reviewer, so a change to the signing allowlist or to a workflow
that carries the HMAC key gets the same review as a docstring fix.
`GOVERNANCE.md:81` already names CODEOWNERS as a two-maintainer control.

**Recommendation.** Add `.github/CODEOWNERS` covering at minimum:
`.github/workflows/`, `config/cicd/`, `src/elspeth/web/sessions/`,
`src/elspeth/web/composer/`, `elspeth-lints/`, and the governance files
(`GOVERNANCE.md`, `SECURITY.md`, `AGENTS.md`). Pair with F-09 — CODEOWNERS is
inert without "require review from Code Owners" in the ruleset.

### F-11 — CONTRIBUTING.md documents no pull-request, branching or review process. MUST CHANGE

Measured section list from `CONTRIBUTING.md` (`grep -n "^## "`, the complete set —
there are exactly eight headings): Development Setup (`:5`), Running Quality
Checks (`:29`), Code Standards (`:52`), Writing Tests (`:59`), Whole-tree gates
(`:93`), Commit Guidelines (`:828`), Reporting Issues (`:834`), License (`:845`).
There is no PR section, no branching model, and no review expectation.

The entire commit-and-land guidance is `CONTRIBUTING.md:830-832`:

> "- Keep commits focused on a single logical change.
> - Write commit messages that explain *why*, not just *what*.
> - Ensure all quality checks pass before committing."

**Why it breaks.** This is complete advice for someone who commits directly to a
branch they own and merges it themselves. It tells a new contributor nothing about
whether to fork or branch, what to target (`main`? `release/0.8.1`? — the repo's
own work lands on the latter), how review happens, who merges, or what "done"
means. A 46 KB contributing guide that covers AST gate internals in depth and omits
"how do I get my change merged" is calibrated for an audience of one.

**Recommendation.** Add a "Proposing a change" section: fork-vs-branch, target
branch (and how to tell which release line a change belongs to — `AGENTS.md:273-275`
already flags that as a decision to check, not infer), PR expectations, required
checks, who reviews, and who merges.

### F-12 — No issue or pull-request templates. SHOULD CHANGE

Measured `.github/` contents: `actionlint.yaml`, `codeql/`, `dependabot.yml`,
`workflows/`. No `ISSUE_TEMPLATE/`, no `PULL_REQUEST_TEMPLATE.md`.

**Why it breaks.** `SUPPORT.md:21-32` ("What Maintainers Need") and
`CONTRIBUTING.md:836-840` both describe what a good report contains — in prose, in
two different files, where a submitter must find and read them first. That works
when the submitter and the reader are the same person. It is also directly
relevant to the migration: GitHub Issues is about to become the system of record,
and the templates are the natural place to encode the label vocabulary from F-31.

**Recommendation.** Create `.github/ISSUE_TEMPLATE/` (bug, feature, question) and
`PULL_REQUEST_TEMPLATE.md`, seeded from the existing `SUPPORT.md:21-32` list, and
wire the label vocabulary in as template defaults.

### F-13 — The ADR template hardcodes a singular decider, so every future ADR inherits it. SHOULD CHANGE

`docs/architecture/adr/000-template.md:5`:

> `**Deciders:** ELSPETH maintainer`

This is the live template, so the singular propagates into every ADR written from
here on. The corpus has already drifted away from it —
`docs/architecture/adr/046-audit-grade-is-a-product-characteristic.md:5` and
`docs/architecture/adr/043-project-tooling.md:333` both say:

> `**Deciders:** ELSPETH maintainers`

**Why it breaks.** The field is load-bearing: it records who has authority to
revisit a decision. With several developers, "ELSPETH maintainer" is ambiguous
between "the person who decided" and "whoever currently holds the role", and those
differ. Because the template is the default, the ambiguity is reproduced by
construction rather than chosen each time.

**Recommendation.** Fix the template to the role name settled in F-02/F-03, and
make the field's meaning explicit (decision-time authority, not current
ownership). Do **not** retro-edit historical ADRs, including the singular in
retired ADR-024 at `:6` — the decider at the time is accurate history and the
template is the only actionable surface.

---

## Theme C — Concurrency (6)

*Framing note: most findings here are about several* agents *sharing one machine,
not about several* people*. They are correct per-host rules and should survive.
They are listed because their wording assumes one owner of that host, which will
read as a project-wide rule to a new developer on their own machine.*

### F-14 — Test-capacity arbitration assumes one person owns the box. DELIBERATE, KEEP — reframe wording

`AGENTS.md:126-129`:

> "- Before starting a full suite on a shared host, establish who owns the test
>   capacity, check for active suites and host load, and run only one broad suite
>   at a time. Use 12 workers only when capacity permits; lower the count when
>   it does not. Do not launch parallel full suites from separate agent sessions."

**Assessment: keep.** "Do not run two 12-worker suites on one 24-CPU box" is a
resource fact, not a governance one, and stays true with any number of developers.

**Why the wording breaks.** "establish who owns the test capacity" is answerable
by asking oneself today. A new developer on their own laptop has no shared host,
and will either apply the rule where it does not apply or conclude the covenant
does not describe their situation.

**Recommendation.** Scope it explicitly: "When several agents share one host
(the maintainer's build box, or any shared CI runner) …". Add that a developer on
a private machine may use full parallelism.

### F-15 — Shared-checkout staging discipline. DELIBERATE, KEEP — reframe wording

`AGENTS.md:193-196`:

> "- Do not silently switch a shared checkout onto a task branch; prefer a
>   dedicated worktree for branch-scoped work and surface the choice first if
>   you must switch. In a shared checkout, stage only your own pathspecs and
>   never `git restore`/`clean` files you did not stage."

**Assessment: keep, emphatically.** This encodes a real data-loss incident and
matters *more* with several actors, not less.

**Why the wording breaks.** "a shared checkout" means "one working directory
shared by several concurrent agent sessions". A new developer will read it as
"the shared repository" and take away a confusing rule about a normal clone, where
`git add .` is unremarkable.

**Recommendation.** Define "shared checkout" in one clause at first use.

### F-16 — The full-suite gate schedules against one physical box. DELIBERATE, KEEP

`scripts/full-suite-gate.sh:167-168`:

> ```
> if [ -z "$WORKERS" ]; then
>     if [ "$N_SIB" -gt 0 ]; then WORKERS=8; else WORKERS=12; fi
> ```

with `scripts/full-suite-gate.sh:43`:

> `#   - counts sibling pytest processes on this box from /proc (never a pgrep`

**Assessment: keep.** Reading `/proc` for siblings is the correct instrument (and
AGENTS.md documents why `pgrep` is not). It degrades gracefully on a machine with
no siblings.

**Why it is listed.** The 12/8 constants are tuned to one specific 24-CPU host.
On a 4-core laptop, 8 workers is a bad default and the gate will look broken.

**Recommendation.** Derive the ceiling from `nproc` rather than hardcoding, or
document the host the constants assume. No behavioural change needed for the
maintainer's box.

### F-17 — Worktree cleanup defaults "landed" to the local main checkout's branch. DELIBERATE, KEEP

`scripts/worktree-cleanup.sh:70`:

> `#   --base REF           ref that defines "landed" (default: the branch the MAIN`

**Assessment: keep.** The flag exists, and the header documents the alternative
(`scripts/worktree-cleanup.sh:66`: `--base origin/main`).

**Why it is listed.** The default is safe only when the local main checkout's
branch is the true integration point. With several developers, a local
`release/0.8.1` may be behind the remote, and "landed" computed against it would
classify unlanded work as removable. The script never force-removes, so the blast
radius is bounded — but the default becomes the wrong one.

**Recommendation.** Change the documented default guidance (not necessarily the
code) to prefer an explicit `--base origin/<integration-branch>` once more than
one person pushes.

### F-18 — `branch-safety-check.sh` tells the user to get permission from "the operator". SHOULD CHANGE

`scripts/branch-safety-check.sh:130`:

> `           else report FAIL protected "$msg; $INTENT needs the operator to name it (or --allow-protected)" "git branch --show-current"; fi ;;`

**Why it breaks.** Same singular as F-03, surfaced in tooling output rather than
prose — so it is what a new developer actually reads at the moment they are
blocked. It also names a person-shaped authority for what is really a branch-policy
question.

**Recommendation.** Reword to point at the branch policy ("`$INTENT` on a protected
branch requires an approved PR; see CONTRIBUTING § Proposing a change"), which will
be true once F-09 and F-11 land.

### F-19 — The `git stash` prohibition is convention-only and self-documents as unenforced. SHOULD CHANGE

`AGENTS.md:200-205`:

> "- Do not use `git stash` — use a worktree or a commit instead. This is a
>   convention, not an enforced gate: nothing blocks it. (A `.git/hooks/pre-stash`
>   once claimed to, but git has no such hook, so it never ran; it was removed
>   2026-09-02 along with the claim that it worked. …)"

**Why it breaks.** The honesty here is exactly right and should be preserved. The
problem is scale: an unenforced convention holds when one person remembers it and
decays with each new contributor. The rationale (a stash is invisible state that
another agent in the same checkout can clobber) is also not stated, so a developer
with their own clone has no way to judge whether it applies to them.

**Recommendation.** Keep the honest "not enforced" note, add the one-line rationale,
and scope it to shared checkouts as in F-15.

---

## Theme D — Onboarding (7)

### F-20 — The tracked Claude Code settings run the maintainer's private tools on every session start. MUST CHANGE

`.claude/settings.json` is **tracked** (measured via `git ls-files`), and
`.claude/settings.json:24-46`:

> ```
>     "SessionStart": [
>       {
>         "hooks": [
>           {
>             "type": "command",
>             "command": "filigree session-context",
>             "timeout": 5
>           },
>           {
>             "type": "command",
>             "command": "filigree ensure-dashboard",
>             "timeout": 5
>           }
>         ]
>       },
>       {
>         "hooks": [
>           {
>             "type": "command",
>             "command": "loomweave hook session-start --path \"${CLAUDE_PROJECT_DIR}\""
>           }
>         ]
>       }
>     ],
> ```

**Why it breaks.** Every fresh clone inherits these hooks. A new developer using
Claude Code gets failing hooks on every session start for two tools they do not
have, have not been told to install, and which `ADR-043` and
`docs/maintainer/toolchain.md:3-5` both say are *not* required to contribute. One
of them is the tracker being retired. This is a hard failure on first contact, and
it directly contradicts the covenant split that ADR-043 performed:
`docs/architecture/adr/043-project-tooling.md:342-343`:

> "ELSPETH is a public repository; a contributor
> should not read a tracker or code-map choice as a condition of contributing."

The split moved the *prose* out of AGENTS.md but left the *executable* form tracked.

**Recommendation.** Move the filigree/loomweave hooks to
`.claude/settings.local.json` (already gitignored and already used for local
overrides). Keep in the tracked file only what is genuinely project-wide — the
`ruff format` PostToolUse hook at `:48-57` and the dangerous-command PreToolUse
guard at `:14-22` both qualify.

### F-21 — `pyproject.toml` names one author, with no email and no maintainers field. SHOULD CHANGE

`pyproject.toml:8`:

> `authors = [{ name = "John" }]`

**Why it breaks.** A first name with no email is not a contactable authorship
record. For a public MIT-licensed package it is also the metadata users see. There
is no `maintainers` field to distinguish original authorship from current
maintenance — a distinction that only starts to matter now.

**Recommendation.** Set `authors` to a full name plus a project contact address,
and add a `maintainers` entry pointing at the role or a shared address. Pairs with
F-24 (SECURITY.md has no mailbox either).

### F-22 — Project-control records exist only outside the repository, reachable by asking one person. SHOULD CHANGE

`docs/project-control/README.md:37-42`:

> "What is here is maintained in full and unsanitised, but it is not published in
> the repository: `.gitignore` excludes everything in this folder except this
> README, so a clone contains only this note.
>
> To read these documents, ask the project maintainer through the channels in
> [SUPPORT.md](../../SUPPORT.md)."

**Why it breaks.** The RAID register, work-package inventory and PRD are the
documents a second developer most needs to understand committed scope, and the
only access route is a request to one person. `ROADMAP.md:161` names exactly this
as a decision that must be settled "Before shared-maintainer operation under F.1":

> `| Key custody and access to project records | Before shared-maintainer operation under F.1 | Determines continuity arrangements and the second maintainer's signing and tooling access. |`

The exclusion may well be correct (these are organisational documents that may
carry commercial content). The gap is that no alternative access path is defined.

**Recommendation.** Decide and record where a second developer gets these — a
private repository, a shared drive with a named access list, or a sanitised
in-repo subset. `ROADMAP.md:161` is the tracking item; make it an issue.

### F-23 — Three of four project-control artefacts do not exist. SHOULD CHANGE

`docs/project-control/README.md:16-21`:

> ```
> | Artefact | State |
> |---|---|
> | Project Control Report (PCR) — current status, exceptions, and asks | Not written |
> | T&M register — resource evidence, allocation, and reconciliation gaps | Not written |
> | RAID register — live risks, assumptions, issues, and dependencies | **Held here** |
> | Milestone and forecast register — commitments, forecasts, and change | Not written |
> ```

**Why it breaks.** Status and forecast can live in one person's head while that
person is the only one delivering. The moment work is shared, "what is the current
status and what is committed" becomes a question several people need answered the
same way. ADR-024 already identified this gap as a symptom
(`docs/architecture/adr/024-delivery-governance-for-single-maintainer-mode.md:18-21`):

> "Two symptoms made that visible. The 2026-08-29 amendment specified four
> living project-control artefacts, and by 2026-09-11 three of them were
> recorded as "Not written" while the artefacts that did exist were not the
> four it named."

**Recommendation.** Either write the three artefacts or amend the README to
describe the set that actually exists. Leaving a table of "Not written" as the
onboarding entry point is the worst of both.

### F-24 — Security disclosure depends on an undefined private channel to one person. MUST CHANGE

`SECURITY.md:21-23`:

> "2. If private vulnerability reporting is not enabled or is temporarily
>    unavailable, contact the repository maintainer through an existing trusted
>    project channel and request a temporary private disclosure path."

and `SECURITY.md:27-30`:

> "As of 0.8.1, a dedicated public security mailbox has not yet been published in
> this repository. GitHub private vulnerability reporting is enabled for this
> repository, so a permanent private disclosure channel is available; publishing
> a dedicated security mailbox remains a public-release readiness item."

**Why it breaks.** "an existing trusted project channel" is undefined — it means
"however you already know how to reach John". A reporter with no prior
relationship has no fallback, and a second maintainer has no defined role in
triage. The document is honest that the mailbox is outstanding; that honesty does
not close the gap.

**Measured, in the project's favour:** the claim at `:28` is **true**.
`gh api repos/dta-au/elspeth/private-vulnerability-reporting` returns
`{"enabled":true}`. (Instrument control: the same token's
`gh api repos/dta-au/elspeth` succeeds with `admin:false`, so this endpoint is
readable without admin and a `true` here is a real reading, not a permissions
artefact — unlike the branch-protection 404 in F-09.) So a permanent private
channel does exist and step 1 of the disclosure path works today.

**Recommendation.** The gap is narrower than the prose suggests: fix step 2, not
step 1. Publish a security address or GitHub team alias to replace "an existing
trusted project channel", and name who monitors it and who is the backup — which
is the same role question as F-02/F-03. Update `:27-30` to state that private
reporting is confirmed enabled rather than leaving it as an assertion.

### F-25 — Push CI runs on a self-hosted runner a new developer cannot reproduce or inspect. SHOULD CHANGE

`.github/workflows/ci.yaml:42`:

> `# Trusted push workflows use the nyx runner.`

`nyx-ci` appears as a runner label in five workflow files and is registered in
`.github/actionlint.yaml:3`.

**Why it breaks.** A contributor whose PR passes on `ubuntu-24.04` cannot predict
or debug the push-event run on `nyx-ci`, cannot inspect that host, and has no way
to tell whether a failure is their change or the runner's state. The design intent
is sound and documented (`ci.yaml:40-42`: PRs stay on hosted runners as a
public-repository guardrail). The gap is that nothing tells a contributor the
two-tier model exists or what to do when the tiers disagree.

**Recommendation.** Document the two-runner model in `CONTRIBUTING.md` — what runs
where, why, and who to contact when a `nyx-ci` job fails for non-code reasons.
Note that F-01's remediation may change which jobs need the trusted host at all.

### F-26 — Quality-check guidance omits the gate agents are told is canonical. SHOULD CHANGE

`CONTRIBUTING.md:31-45` presents the required checks as four commands
(`pytest`, `mypy`, `ruff`, `check_contracts`). `AGENTS.md:96-102` and
`CLAUDE.md:21` instead make `scripts/full-suite-gate.sh --execute --detach` the
canonical pre-merge gate, and `CLAUDE.md:14-16` says these tasks "must not be
improvised from raw git or pytest commands".

**Why it breaks.** Two audiences are given two different answers to "what must pass
before I submit". One person holding both in their head is fine; a new developer
following `CONTRIBUTING.md` runs a weaker gate than the project considers
authoritative, and will not know it.

**Recommendation.** Point `CONTRIBUTING.md § Running Quality Checks` at the
canonical script, keeping the individual commands as the "what it runs" breakdown.

---

## Theme E — Tracker (7)

*Standing context, measured 2026-09-23: **the migration is already underway and
tracked.*** `docs/github-issues/` exists in the index (`git ls-files` returns
`README.md`, `STYLE.md`, `check_issues.py`, `held/`, `issues/`) and carries a
staging directory, a house style with redaction rules, a fail-closed
pre-publication gate, and an importer. Its README states the premise of this
entire audit independently — `docs/github-issues/README.md:3-5`:

> "ELSPETH tracked its work in **filigree**, an agent-native issue tracker with a local
> database. That fitted a project with one developer and a fleet of agents. It does not fit
> a project with several developers, so GitHub Issues becomes the system of record."

**The findings below are therefore scoped to what that migration does not already
cover.** Two of my original recommendations are superseded by work already done,
and I have marked them inline: the id-mapping mechanism I proposed in F-28 already
exists as `.import-state.jsonl` (`docs/github-issues/README.md:85-88`), and the
redaction concern implicit in exporting tracker prose is already handled by
`check_issues.py`, which `README.md:24-25` says "encodes exactly the patterns that
were actually found, and it fails the build rather than warning". The remaining
tracker findings — the AGENTS.md installer block (F-27), the atomic-claim rule
(F-29), and the tracked skills (F-30) — are **not** in that migration's scope,
because it stages *issues*, not the repository's standing agent instructions.

### F-27 — The covenant names Filigree as the system of record. MUST CHANGE

`AGENTS.md:377-379`:

> "`filigree` tracks this project's work. Use it to find, claim, update and close
> issues: `filigree session-context` at session start, then
> `filigree start-next-work --assignee <name>`."

**Why it breaks.** This is the covenant — the file every agent and contributor
reads first — asserting that the retired tracker is authoritative. After the
migration it is simply false, and it is false in the one document most likely to
be trusted without checking.

**Note on mechanics.** This block is installer-managed. `AGENTS.md:373-374`:

> ```
> <!-- filigree:instructions:v3.1.0:c1c023c3 -->
> <!-- filigree:last-writer:filigree install -->
> ```

and `docs/maintainer/toolchain.md:13-17` warns:

> "The Filigree and Loomweave blocks
> below are installer-written mirrors: their installers rewrite the block
> between the `<!-- <tool>:instructions -->` markers on every run."

So deleting the block is not enough — a later `filigree install` would restore it.

**Recommendation.** Uninstall Filigree's AGENTS.md integration (its own uninstall
verb, per `AGENTS.md:305-307` on tool hygiene) rather than hand-deleting the block,
then replace the section with a GitHub Issues pointer. `ADR-043` requires that
adding or removing a tool carrying standing agent instructions is a recorded
decision — so this needs an ADR amendment, not just an edit.

### F-28 — 301 tracker ids in tracked prose become dangling pointers. MUST CHANGE

Measured (instrument controlled — see header): **301 unique `elspeth-[0-9a-f]{10}`
ids** in tracked Markdown, excluding `docs-archive/`, `notes/`, `CHANGELOG.md`,
`docs/plans/`, `docs/reviews/` and `docs/agents/`. Highest-density files:

| File | Occurrences |
|---|---|
| `docs/architecture/adr/025-multi-source-ingestion.md` | 72 |
| `docs/architecture/adr/026-durable-token-scheduler.md` | 50 |
| `docs/architecture/adr/010-declaration-trust-framework.md` | 25 |
| `docs/programmes/identity-workflow-governance/README.md` | 19 |
| `docs/specs/2026-09-02-pluggable-sso-design.md` | 16 |
| `docs/architecture/adr/029-journal-is-barrier-buffer-truth.md` | 14 |

`AGENTS.md` itself carries four (`:132`, `:142`, `:345`, `:348`), e.g. `AGENTS.md:142`:

> "  parallelism (elspeth-0077cb7789): two runs of identical code produced"

**Why it breaks.** These are load-bearing citations — the evidence for why a rule
exists. Once Filigree is gone, a reader who wants to check a claim hits an
identifier with no resolver. That is worse than no citation, because it looks
verifiable.

**Recommendation.** Do not mass-rewrite. Part of this is already solved: the
migration writes `.import-state.jsonl` mapping every slug to its issue number
(`docs/github-issues/README.md:85-88`), which is the resolver substrate. Build on
it rather than duplicating it: (a) extend that state file, or a note beside it, to
carry the **old `elspeth-<hex>` id** alongside the slug, so the 301 in-tree
citations resolve forward; (b) preserve a read-only export of the Filigree
database for the ids that are never migrated — the 72 in ADR-025 and 50 in ADR-026
are closed history and will have no issue number; (c) rewrite ids in prose only in
the small set of living documents — `AGENTS.md`, `CONTRIBUTING.md`, and any ADR
still being amended. Historical ADR citations should stay as history.

**Timing matters.** Step (b) must happen before the Filigree database is
decommissioned. Once it is gone, the ~300 ids that were never imported become
permanently unresolvable, and that is the irreversible half of this finding.

### F-29 — Atomic-claim protocol has no GitHub equivalent and will mislead. MUST CHANGE

`AGENTS.md:385-391`:

> "Two rules `--help` will not tell you:
>
> 1. Claim atomically: `work_start` / `work_start_next` (MCP) or `start-work` /
>    `start-next-work` (CLI). Never chain a claim with a separate status update;
>    that two-step form races other agents.
> 2. On `SCHEMA_MISMATCH` the installed filigree is older than the project
>    database. Surface it to the user; do not retry."

**Why it breaks.** The race this prevents is real and becomes *more* likely with
more actors — but GitHub Issues has no atomic claim-and-transition verb. Assignment
and label changes are separate API calls. So the rule cannot be followed after the
migration, and the underlying hazard it guards against is left unaddressed.

**Recommendation.** Replace with a GitHub-shaped convention: assign-then-comment,
or a single "in progress" label applied at assignment time, and state plainly that
GitHub cannot make this atomic so duplicate pickup is possible — check assignment
before starting. Preserving the *warning* matters more than preserving the verb.

### F-30 — Tracked skills teach the retired tracker. MUST CHANGE

Both `.agents/skills/filigree-workflow/` and `.claude/skills/filigree-workflow/`
are tracked, with five reference files each. `.agents/skills/filigree-workflow/SKILL.md:15`:

> "project. This skill is procedural knowledge for using it well — as a solo agent"

`.agents/skills/cicd-allowlist-audit/SKILL.md` and
`.agents/skills/bug-sweep/SKILL.md` also depend on Filigree for lodging findings —
`.agents/skills/bug-sweep/SKILL.md:9`:

> "  what you find", or any large read-only review that must scale past one agent."

**Why it breaks.** These are invoked by name and will drive agents to a tracker
that no longer exists. `bug-sweep` in particular is structured around "lodge
verified findings in Filigree under one sweep tag" — its whole reconciliation step
breaks.

**Recommendation.** Retarget `bug-sweep` and `cicd-allowlist-audit` at GitHub
Issues (labels replace sweep tags naturally). Retire `filigree-workflow` or rewrite
it as `github-issues-workflow`. Note the "solo agent" phrasing at `:15` should go
regardless.

### F-31 — The label vocabulary exists only inside the tracker database. SHOULD CHANGE

`docs/agents/tracker-label-vocabulary.md:1-5`:

> "# Tracker label vocabulary (`p1-class:*`, `lane:*`)
>
> Filigree label namespaces whose meaning lives only in the tracker database.
> Written 2026-08-17 because the vocabulary had no definition anywhere in the
> tree, so every session re-derived it by sampling issues."

**Measured correction to this finding.** The migration has **deliberately
designed a replacement taxonomy**, and it does not include this vocabulary.
`docs/github-issues/STYLE.md:127-129`:

> "- `labels` — pick from: `area/composer`, `area/engine`, `area/plugins`, `area/web`,
>   `area/cli`, `area/audit`, `area/deployment`, `area/tests`, plus exactly one of
>   `type/bug`, `type/task`, `type/epic`. Add `needs-triage` if the source is unclear."

followed at `:131` by "Do not add any other front-matter key. The importer reads
only these two." Confirmed by measurement: `grep -rn "p1-class\|lane:"
docs/github-issues/` returns nothing. So recreating `p1-class:*` and `lane:*` as
GitHub labels — which is what I would otherwise have recommended — would
contradict a considered decision, and I withdraw it.

**The real gap is narrower and worth raising.** The new scheme carries *subject*
(`area/*`) and *kind* (`type/*`). It carries **no impact or priority axis at
all**. The retired `p1-class:*` vocabulary was explicitly that axis —
`docs/agents/tracker-label-vocabulary.md:9`:

> "A closed 7-value vocabulary answering *what does it cost if this is not done*."

With one developer, priority can live in one person's head. With several, "what
should I pick up next" and "does this block the release" are exactly the questions
a shared tracker has to answer, and `area/` + `type/` cannot answer either.

**Recommendation.** Confirm with whoever owns the migration whether dropping the
impact axis was intentional or incidental. If intentional, record why in
`STYLE.md` so it is not re-litigated. If incidental, add a small priority axis
(`P0`–`P4` already exists as a scale at `docs/maintainer/toolchain.md:160-166`)
rather than reviving the seven-value `p1-class:*` set, which was tuned to a
release horizon that has passed. Retarget or retire
`docs/agents/tracker-label-vocabulary.md` either way — it currently documents a
vocabulary with no live consumer.

### F-32 — GOVERNANCE.md points at an unnamed tracker. SHOULD CHANGE

`GOVERNANCE.md:30`:

> "- the project issue tracker for active release blockers and follow-up work."

**Why it breaks.** In a governance document that a reader consults to learn where
decisions are recorded, "the project issue tracker" resolves to nothing. It was
unambiguous when there was one tracker and one user.

**Recommendation.** Name GitHub Issues with a URL, as the sibling bullets at
`:27-29` do for `docs/architecture/adr/` and `docs/contracts/`.

### F-41 — The work that makes the project safe for several developers is tracked where several developers cannot see it. MUST CHANGE

`docs/github-issues/README.md:53-60`:

> "## What is not migrating
>
> Two categories stay out of GitHub, both by operator ruling on 2026-09-23.
>
> **Internal governance** — the trust-tier allowlist burn-downs, judge-signing tooling, the
> lint gate's own internals, and the agent tooling. These are the project's own machinery
> rather than product defects, and they mean nothing to an outside contributor. They keep
> the `exclude:gh-migration` label in filigree."

**The ruling is reasonable on its own terms.** Lint-gate internals and agent
tooling genuinely mean nothing to an outside contributor, and ADR-046 already
says project tooling is not product. Nothing here argues with that.

**Why it breaks anyway.** Two consequences follow that the ruling does not appear
to have weighed:

1. **Filigree is not actually retired.** It remains the system of record for one
   class of work. So the project is heading for a **two-tracker state** — GitHub
   for product defects, a local private database for governance — which no
   document currently describes. `AGENTS.md:377` ("`filigree` tracks this
   project's work") becomes half-true rather than false, and a new developer has
   no way to learn which half.
2. **The category is drawn in the wrong place for this transition.** Judge-signing
   tooling and the trust-tier allowlist are "internal governance" by this
   taxonomy — and they are also **F-01, F-02, F-03 and F-04**, four of the most
   serious findings in this audit, including a key-exposure path. Work to make the
   repository safe for several developers would therefore be tracked in a local
   database on one person's machine that those developers cannot read, cannot be
   assigned in, and cannot see the status of. That is a single-developer
   assumption reproducing itself inside the fix for single-developer assumptions.

The distinction that actually matters is not product-versus-tooling. It is
**"does a second developer need to see this to work safely"** — and for key
custody, branch protection and the signing role, the answer is yes.

**Recommendation.** Keep the ruling's intent, but split the excluded category:

- **Stays out of GitHub:** lint-gate internals, allowlist burn-down mechanics,
  agent-tooling maintenance. Genuinely internal, genuinely uninteresting
  externally. No change.
- **Moves into GitHub:** anything a second developer must see to work safely —
  key custody and the signing role (F-02, F-03, F-04), branch protection and
  review controls (F-08, F-09, F-10), and the CI key exposure (F-01). These are
  governance *decisions and access*, not tooling internals. F-01 in particular
  should not sit in a private tracker; it is a live exposure with a named
  remediation.
- **Either way, document the split.** If any tracker other than GitHub Issues
  remains a system of record for anything, `AGENTS.md` and `GOVERNANCE.md:30`
  must say so explicitly, with the boundary stated. An undocumented second
  tracker is worse than either tracker alone.

If F-01's remediation is considered sensitive enough to keep off a public tracker,
that is a legitimate call — but then it needs a private channel a second
maintainer can actually reach, which is the same unresolved question as **F-24**
and **F-22**.

### F-33 — Contributor-facing and agent-facing docs already disagree about the tracker. SHOULD CHANGE

`SUPPORT.md:11-14` and `CONTRIBUTING.md:836` already direct people to GitHub:

> "- For reproducible defects, open a GitHub issue with version, commit, command,
>   expected behaviour, actual behaviour, and relevant logs."

while `AGENTS.md:377` (F-27) directs agents to Filigree.

**Why it breaks.** Contributors file in one system, the maintainer's agents work
another, and nothing reconciles them. With one person bridging both, nothing is
lost. With several, externally-filed issues and internally-tracked work drift apart
silently.

**Recommendation.** The migration resolves this by construction — the point is to
verify it afterwards, and make GitHub Issues the single named target in all four
documents.

---

## Theme F — Prose & framing (5)

### F-34 — "maintained by a single developer" is load-bearing, not decorative. MUST CHANGE

`AGENTS.md:284` (HEAD line 278):

> "ELSPETH is pre-release software maintained by a single developer. Keep a
> process, gate, or document only when it materially improves at least one of:"

**Why it breaks.** This sentence is the premise of the entire § Project delivery
posture — the justification for rejecting approval chains, role handoffs, and
review receipts (`AGENTS.md:295-299`). Those rejections are *correct* for
disposable working documents and should largely survive. But the premise is about
to be false, and several of the named "ceremony" items (role handoffs, approval
chains) are exactly what F-07 through F-11 will reintroduce, deliberately, for
code.

Note the section already draws the right line at `AGENTS.md:297-299`:

> "This does not prohibit
> signatures, checksums, audit chains, or admission gates that protect actual
> code, releases, exports, runtime data, or deployed artifacts."

**Recommendation.** Restate the premise as "a small team" and rewrite the test to
lean on that existing distinction: ceremony for disposable documents stays
prohibited; controls that protect code and releases are expected to grow. Keep the
three-part reliability/integrity/supportability test verbatim — it scales fine.

### F-35 — "the developer" as a singular authority appears in three binding rules. MUST CHANGE

`AGENTS.md:267-269`:

> "- The reported defect is the deliverable. Do not ship a cosmetic or adjacent
>   change while deferring the actual fix into new tickets unless the developer
>   approved that split first."

`AGENTS.md:299-301`:

> "If removing a
> practice is a marginal call or may discard a real safeguard, surface the tradeoff
> to the developer before removing it."

`AGENTS.md:316-318`:

> "Two rules govern every change to the Web Composer. Neither is subject to a
> latency, cost, or convenience argument. If you believe you need an exception,
> STOP and ask the developer before writing code."

**Why it breaks.** Each is an escalation path to an unnamed singular person. With
several developers, "ask the developer" is ambiguous — the one you are pairing
with? the one who owns the composer? — and the composer invariants at `:314-355`
are precisely where an ambiguous escalation is most dangerous, since they exist to
stop a plausible-sounding shortcut from landing.

**Recommendation.** Replace with a named role or a mechanism: "ask on the issue" /
"request review from the composer code owner" (which CODEOWNERS from F-10 would
make concrete). The composer rule should escalate to a reviewer with authority, not
to a person by definite article.

### F-36 — The covenant's pointer to the maintainer toolchain implies one agent operator. SHOULD CHANGE

`AGENTS.md:12-15` and, near-verbatim, `CLAUDE.md:10-12`:

> "This file is the harness-neutral covenant for any agent or contributor. The
> maintainer's own agent toolchain (issue tracker, code map, delegation
> conventions) is described in [docs/maintainer/toolchain.md](docs/maintainer/toolchain.md);
> none of it is required to contribute."

**Assessment.** The *split* is right and ADR-043 argued it well. The framing is
what dates: "the maintainer's own" presumes one person's setup is the only
non-standard one. A second developer with their own tooling has nowhere to record
it, and no guidance on whether they may add hooks or skills.

**Recommendation.** Rename to "personal agent toolchains" (plural) and state that
contributors may keep their own, with the rule that anything carrying *standing
agent instructions* in tracked files is a recorded decision under ADR-043 — which
is the actual invariant worth preserving.

### F-37 — The standing dispatch authorization is one person's grant, reproduced as project text. SHOULD CHANGE

`docs/maintainer/toolchain.md:27-28`:

> "This is the maintainer's grant to their own agents. It is reproduced verbatim
> because the agents act on the wording."

`docs/maintainer/toolchain.md:30-33`:

> "**Use skills, subagents, nested subagents, and multi-agent workflows liberally
> and at your own discretion.** This is a standing user request, not a
> conditional permission: spawn without asking, at whatever depth and fan-out the
> work warrants."

and `AGENTS.md:276-280` points the covenant at it:

> "- This is not a budget cap. Wide dispatch and deep analysis are standing
>   policy for the maintainer's own agents
>   ([docs/maintainer/toolchain.md](docs/maintainer/toolchain.md) § Standing
>   authorization); this section constrains what you hand back, not what you
>   spend getting there."

**Why it breaks.** This grant spends money and shared compute. One person granting
it to their own agents on their own box and their own account is coherent. With
several developers it is unclear who may issue it, whether it applies to a new
developer's agents, and who absorbs the cost — while
`docs/maintainer/toolchain.md:67-74` simultaneously insists no dispatch constraint
"may be re-accumulated" and that the box's finite resources still bind.

**Recommendation.** Keep the grant, scope it explicitly to the granting developer's
own agents and own resources, and state that another developer's agents operate
under whatever that developer authorises. Pair with F-14's per-host ceiling.

### F-38 — A retired ADR remains the cited authority in three sibling ADRs. SHOULD CHANGE

`docs/architecture/adr/024-delivery-governance-for-single-maintainer-mode.md:41-46`:

> "Sibling ADRs that cite ADR-024 in their *Related Decisions* sections
> ([ADR-025](025-multi-source-ingestion.md),
> [ADR-026](026-durable-token-scheduler.md),
> [ADR-029](029-journal-is-barrier-buffer-truth.md)) cite it only as the
> governance context in force at the time. Those references remain accurate as
> history; read them against GOVERNANCE.md for the current posture."

**Assessment.** The handling is careful and the redirect table at `:33-39` is good
practice. This is the mildest finding in the audit.

**Why it is listed.** A reader arriving at ADR-025's Related Decisions sees a link
to a document whose title still reads "Delivery Governance for Single-Maintainer
Mode". After the transition, that title describes a mode the project has left, and
only someone who opens the file learns it was retired in 2026-09.

**Recommendation.** When GOVERNANCE.md is rewritten for multi-developer mode, add
one line to ADR-024's header noting the posture it described has since been
superseded in practice, not only in filing. Leave the sibling citations alone.

---

## Prioritised: what to change first

**Before a second developer is granted push access** — these are unsafe to defer:

1. **F-01** — protect `release/**`, and move `ELSPETH_JUDGE_METADATA_HMAC_KEY`
   behind a GitHub Environment with required reviewers. Until this lands, push
   access *is* key access.
2. **F-09** — branch protection on `release/**` (the mechanical half of F-01;
   verify with an admin token and record the output, per GOVERNANCE.md:73-75).
3. **F-10** — add `.github/CODEOWNERS`, weighted to `.github/workflows/`,
   `config/cicd/`, and the web session/composer trees.
4. **F-07 / F-08** — flip GOVERNANCE.md out of single-maintainer mode and enable
   the six controls it already specifies.

**Before the tracker migration cuts over:**

4a. **F-41** — decide where governance work is tracked. The operator ruling at
   `docs/github-issues/README.md:55-60` keeps it in filigree, which would put
   F-01's remediation in a private local database a second developer cannot read.
   This decision gates items 1–4 above, so make it first.
5. **F-27 / F-30** — uninstall the Filigree AGENTS.md block (via its own uninstall
   verb, not by hand) and retarget the tracked skills; record it as an ADR-043
   amendment. Note this is only correct if F-41 resolves toward a single tracker;
   if filigree is retained for governance, the block needs rewriting rather than
   removing.
6. **F-31** — create the GitHub labels from the existing vocabulary document
   *before* migrating issues, or the classification is lost in transit.
7. **F-28** — commit a read-only Filigree export plus a resolver note, and carry
   old ids into migrated issue bodies. Do this before the database goes away.
8. **F-29** — replace the atomic-claim rule with a GitHub-shaped convention that
   preserves the warning.

**Before the first external contribution:**

9. **F-11** — a "Proposing a change" section in CONTRIBUTING.md.
10. **F-20** — move the filigree/loomweave session hooks out of the tracked
    `.claude/settings.json`.
11. **F-24** — publish a security contact and name who monitors it.
12. **F-12** — issue and PR templates.

**Then, as considered work:**

13. **F-02 / F-03 / F-04** — define the signing-custodian role, name a backup,
    document what a custodian needs, and record the tradeoff on moving judge
    metadata to the Ed25519 keyring model already used for ECS approvals.
14. **F-34 / F-35 / F-36 / F-37** — the covenant prose pass. Do this *after* the
    control changes, so the text describes what is actually true.
15. **F-14 through F-19** — scope the concurrency rules to "shared host" /
    "shared checkout" wording. Low risk, low urgency, improves every future
    onboarding.
16. **F-21 / F-22 / F-23 / F-25 / F-26 / F-32 / F-33 / F-38** — housekeeping.

---

## Deliberate, and worth keeping

These look like single-developer artefacts and are not. Each should survive the
transition, with the documentation note given.

- **Single custodian for the judge-signature key (F-02).** Restricting a
  forgery-capable symmetric key to one holder is correct at any team size. What
  must change is that it becomes a *named role* with a *named backup*, and that
  the project decides whether to keep HMAC (unattributable beyond the key) or move
  to the per-signer Ed25519 model it already ships at
  `docs/runbooks/aws-ecs-deployment.md:608-611`. Adding a second HMAC holder
  without that decision is the one clearly wrong option.

- **The deliberate fail-closed trust-tier gate (`AGENTS.md:359-363`).** "Do not
  attempt to resolve, re-sign, restage, or otherwise clear the trust-tier CI
  failure globally during ordinary feature work" reads like one person's private
  arrangement, but it is a real admission control: it blocks merges while keeping
  outstanding signing work visible. It gets *more* valuable with more contributors,
  since more people could otherwise paper over it. Keep verbatim; only the
  "operator signs" clause needs the F-03 role rename.

- **Per-host test-capacity and shared-checkout rules (F-14, F-15, F-16, F-17).**
  These are resource and concurrency facts about several agents on one machine,
  not governance. They should be *scoped* ("when several agents share one host"),
  not removed. The `/proc`-based sibling count in `full-suite-gate.sh` and the
  `/proc`-based IN-USE detection in `worktree-cleanup.sh` are good instruments and
  the AGENTS.md notes explaining why `pgrep` was rejected are worth preserving.

- **The `git stash` prohibition's honesty (F-19).** `AGENTS.md:200-205` documents
  that the hook which claimed to enforce it never ran, and says so plainly. That
  candour about a failed control is a project norm worth protecting; the rule needs
  scoping and a rationale, not deletion.

- **The AGENTS.md / toolchain.md split itself (ADR-043).** The instinct — keep the
  public covenant harness-neutral, push personal tooling into a labelled document
  that disclaims itself — is exactly right for a multi-developer project. F-20 and
  F-36 are about finishing that split (the executable config and the singular
  framing were left behind), not reversing it.

- **The delivery-posture test (`AGENTS.md:285-289`).** "Keep a process, gate, or
  document only when it materially improves reliability, integrity, or
  supportability" is a good test at any team size, and its carve-out at `:297-299`
  already permits the controls this audit recommends adding. Only the
  "single developer" premise sentence needs rewriting (F-34) — the test survives
  intact.

---

# Addendum, 2026-09-23 — controls 1–4 are now MEASURED, and all six are ABSENT

The audit above asks, as its single missing piece of evidence, that "someone with
admin should run `gh api repos/dta-au/elspeth/branches/main/protection` and record
the output". The maintainer subsequently authorised use of the `johnm-dta` account
for the GitHub migration. That account holds
`{"admin":true,"maintain":true,"push":true,"triage":true}` on this repository, so
the command was run. This addendum records the output and corrects the table.

## What the command returned

```
$ gh api repos/dta-au/elspeth/branches/main/protection
{"message":"Branch not protected", ... "status":"404"}
exit=1
```

**A 404 under an admin token is a substantive answer, not a permission ceiling.**
The audit was right to refuse to read the earlier 404 that way — under
`admin:false` the two cases are genuinely indistinguishable — but with admin the
ambiguity is gone: `main` carries no *classic* branch protection.

It is nonetheless reported as `protected: true` by the branches API. The
protection is a **repository ruleset**, which the classic endpoint does not
report:

```
$ gh api repos/dta-au/elspeth/rulesets
[{"id":12348893,"name":"main","target":"branch","enforcement":"active", ...}]
```

## The ruleset, rule by rule

Ruleset 12348893, `enforcement: active`, `conditions.ref_name.include:
["~DEFAULT_BRANCH"]`, `bypass_actors: []`, `current_user_can_bypass: "never"`.

| Rule | Parameter | Value |
|---|---|---|
| `pull_request` | `required_approving_review_count` | **0** |
| `pull_request` | `dismiss_stale_reviews_on_push` | **false** |
| `pull_request` | `require_last_push_approval` | **false** |
| `pull_request` | `required_review_thread_resolution` | **false** |
| `pull_request` | `require_code_owner_review` | **false** |
| `pull_request` | `require_extra_approval_for_unattributed_changes` | true |
| `required_status_checks` | contexts | `CI Success`, `CodeQL`, `Check cohort-attribution trailers on PR commits`, `redaction-gate` |
| `required_status_checks` | `strict_required_status_checks_policy` | true |
| `deletion`, `non_fast_forward` | — | present |

## The corrected table

| # | Control (GOVERNANCE.md:77-82) | Prior state | **Measured state** | Evidence |
|---|---|---|---|---|
| 1 | one required approving review | Unmeasured | **ABSENT** | `required_approving_review_count: 0` |
| 2 | stale-review dismissal on new commits | Unmeasured | **ABSENT** | `dismiss_stale_reviews_on_push: false` |
| 3 | last-push approval protection | Unmeasured | **ABSENT** | `require_last_push_approval: false` |
| 4 | required conversation resolution | Unmeasured | **ABSENT** | `required_review_thread_resolution: false` |
| 5 | CODEOWNERS / ownership map | ABSENT | **ABSENT** (confirmed twice) | no CODEOWNERS file; `require_code_owner_review: false` |
| 6 | review requirements for release branches | ABSENT | **ABSENT** | ruleset targets `~DEFAULT_BRANCH` only; all six `release/*` branches `protected: false` |

**All six named two-maintainer controls are now measured, and all six are absent.**
The audit's split of "2 absent, 4 unmeasured" is superseded. No finding is
withdrawn; F-09 and F-10 are unchanged, and F-07/F-08 are upgraded from a stated
limit to a measurement.

## Two things this changes

**It sharpens F-01 rather than softening it.** The ruleset condition is
`~DEFAULT_BRANCH` — `main` alone. Re-measured the same day:

```
release/0.6.0  protected=false      release/0.7.1  protected=false
release/0.6.1  protected=false      release/0.8.0  protected=false
release/0.7.0  protected=false      release/0.8.1  protected=false
```

So the branches onto which `ci.yaml` injects the judge HMAC key are exactly the
branches with no rule of any kind. The fix named in the audit — a wildcard
ruleset over `release/**` — is now a concrete edit to an existing, working
mechanism rather than a green-field configuration.

**Some of the posture is stronger than the audit could see.** Three controls
nobody claimed are in force on `main`: no deletion, no force-push, and
`bypass_actors: []` with `current_user_can_bypass: "never"` — the ruleset binds
administrators too, which is the property that makes the required checks
meaningful rather than advisory. `CodeQL` and `redaction-gate` are required
contexts that `GOVERNANCE.md` does not mention at all.

The `pull_request` rule with zero required approvals is also not nothing: it
**requires a pull request** to land on `main`, so direct pushes to the default
branch are already blocked. That is the honest shape of single-maintainer mode —
the process gate exists, the human-approval gate is deliberately empty — and it
means the step-up for controls 1–4 is four boolean changes to one existing
ruleset, not a new control surface.

## Limit of this addendum

Organisation-level rulesets were **not** readable: `gh api orgs/dta-au/rulesets`
returned 404 with `This API operation needs the "admin:org" scope`. An org-level
ruleset could add controls not visible here. It could not remove any of the above,
so every "ABSENT" verdict stands, but a stricter org policy may exist unmeasured.
