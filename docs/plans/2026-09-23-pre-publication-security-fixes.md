# Pre-publication security fixes — brief

Two defects are held back from the public GitHub issue tracker because this project's own
rule (`docs/github-issues/README.md`) says an unfixed vulnerability belongs in a private
advisory and becomes a public issue once it is fixed. Both must be fixed before
`dta-au/elspeth` gets its next batch of issues. The rest of the migration is already
published as `#158`–`#201`.

Context for both: ELSPETH is pre-release, so live exposure is negligible today. It is
approaching official release with more than one developer, which is exactly the transition
that turns both of these from theoretical into real.

---

## Work item 1 — the judge HMAC key is reachable by anyone who can push to a release branch

**Severity: highest. Fix this first.** Audit reference: `F-01` in
`docs/reviews/2026-09-23-single-developer-assumptions.md`.

### What is true, measured

- `.github/workflows/ci.yaml:14-15` triggers on push to `main`, `RC*` and `release/**`.
- `ci.yaml:455`, `:472` and `:619` each inject
  `ELSPETH_JUDGE_METADATA_HMAC_KEY` gated on `github.event_name != 'pull_request'` —
  that is, the key is present precisely when there is **no** pull request.
- `ci.yaml:60` sends those same non-PR runs to the self-hosted `nyx-ci trusted` runner.
- All six `release/*` branches are `protected: false`. Only the default branch is covered,
  by repository **ruleset** `12348893`, whose condition is `~DEFAULT_BRANCH`.
- The stated threat model (`docs/judge-signature-handoff.md:10-13`) is that any holder of
  the key can forge `judge_verdict: ACCEPTED` and pass every gate.

### Why it is exploitable

A workflow runs from the ref that was pushed. So anyone with push access can push an edited
`ci.yaml` to a release branch and get arbitrary execution on the maintainer's own hardware
with the key in the environment. The documented mitigation
(`docs/judge-signature-handoff.md:14-18`) is accurate but narrower than it reads: it
addresses **PR**-controlled code. This is **push**-controlled code.

### Before proposing a fix, answer these

1. **Does CI need the key at all?** Signing never runs in CI — the operator fires it
   locally. Establish what the three key-bearing steps actually do with it. If they perform
   full HMAC verification, say what breaks if they run shape-only
   (`ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing`) and a
   trusted context re-verifies before merge — which is already the treatment CI gives fork
   PRs. **Removing the key from CI entirely may be the smallest correct fix.** Do not assume
   it is; measure what the steps need.
2. **What would a `release/**` ruleset cost?** The maintainer pushes directly to
   `release/0.8.1` constantly. A ruleset that requires a pull request there would change the
   daily workflow substantially. A ruleset that restricts *who may push* without requiring a
   PR may achieve the security goal at far lower cost. Price both.
3. **Is a GitHub Environment with required reviewers viable** for the key-bearing jobs? This
   decouples key access from push access and survives a future protection misconfiguration,
   which repository-scoped `secrets` does not.

### Traps

- **`gh api repos/dta-au/elspeth/branches/main/protection` returns 404 "Branch not
  protected" even though the branch IS protected.** The protection is a *ruleset*; the
  classic endpoint does not report rulesets. Always also read
  `gh api repos/dta-au/elspeth/rulesets`. A 404 here under a non-admin token is a permission
  ceiling and means nothing either way.
- Admin is required to read or write protection. The `johnm-dta` account has
  `admin: true`; the other account on this machine does not. Pass it inline —
  `GH_TOKEN="$(gh auth token -u johnm-dta)" gh ...` — rather than `gh auth switch`, which
  flips global state that concurrent sessions share.
- Organisation-level rulesets need `admin:org` and were not readable. A stricter org policy
  may exist. Check before concluding something is absent.

### Scope boundary

**Do not change any repository or organisation setting without an explicit go-ahead.** These
are outward-facing changes on a government-owned public repository. Produce the exact
commands and the expected before/after, show them, and wait. Editing `ci.yaml` in the working
tree is ordinary work and needs no special permission.

### Documentation debt to close in the same change

Two tracked files claim broader coverage than the configuration delivers. Both must end up
true:

- `docs/judge-signature-handoff.md:14-18` — describes the PR gate as *the* CI-exposure
  mitigation.
- `.github/workflows/enforce-allowlist-judge-gates.yaml:41` — the comment reads "must never
  be reachable from PR-controlled code", which states the intent more broadly than the
  configuration achieves.

---

## Work item 2 — sentinel custody projection fails open

Audit reference: the held issue at
`docs/github-issues/held/sentinel-custody-projection-fails-open-when-live-source-op.md`.
**Read that file first — it is the specification**, is written for someone with no prior
context, and defines every term.

### The defect in one line

When a live source's options carry a file path but no `blob_ref`, custody validation passes
without checking blob identity, and the code then stamps the **approved** blob's sentinel
onto a source that reads a **different** blob's bytes — an affirmative, false custody claim
on surfaces that users and the model provider can see.

### Where the work is

- `src/elspeth/web/composer/guided_blob_refs.py` —
  `validate_guided_reviewed_sentinel_source_mapping`. Its only identity comparison is guarded
  by `"blob_ref" in options`. Every other check is a shape check. With the key absent, the
  guard is skipped and the mapping validates.
- `src/elspeth/web/composer/redaction.py` — `redact_guided_snapshot_storage_paths` then
  substitutes the reviewed sentinel on the strength of that validation.
- `src/elspeth/web/sessions/routes/_helpers.py` — the comment beside the redaction call
  records that the manual `set_source` commit path strips `blob_ref` *precisely because* it
  cannot prove `path` equals the blob's `storage_path`. **The codebase produces the
  vulnerable shape itself; this is not hypothetical.**

### The design decision that comes first

This is why it is not a quick change. With `blob_ref` absent there are two defensible
behaviours and someone has to choose:

1. **Establish identity another way** — for example require that the live path equals the
   reviewed blob's `storage_path`. Keeps more sources working; narrows rather than closes.
2. **Degrade to the custody-unavailable outcome** the fail-closed arm already produces.
   Simpler and provably safe; refuses some sources that would have worked.

State your recommendation with the reasoning, and **surface the choice before implementing
it**. What must not remain reachable either way: stamping the reviewed sentinel onto a source
whose identity was never checked.

Note the direction of failure is what makes this worse than its neighbours on the same path.
They fail **closed** — a 500, too late, but no false claim. This one fails **open** and emits
a claim that is not true into the surfaces the audit trail exists to make trustworthy.

### Done looks like

A new test arm directly beside
`tests/unit/web/composer/test_redact_set_source.py::test_redact_guided_snapshot_rejects_live_blob_ref_conflicting_with_reviewed_sentinel`,
which already pins the case where `blob_ref` is **present** and disagrees. The missing arm is
the same scenario with `blob_ref` **absent**: it must assert the output carries either a
verified sentinel or the custody-unavailable outcome, and never the reviewed sentinel over
unverified bytes.

**Control the test before trusting it.** Confirm it fails against the current code for the
right reason, then passes after the fix. A test that passes on both trees proves nothing.

---

## Rules that apply to both

- `AGENTS.md` is the covenant; read it. In particular: measure every claim with the command
  shown beside it, and control any ad-hoc instrument against a known-positive and a
  known-negative before trusting a number it produced. A grep that silently matches nothing
  returns exactly what a correct one returns when there is nothing to find.
- Do not resolve, re-sign or clear the trust-tier CI failure. It is a deliberate fail-closed
  state. Fix tier-model defects you touch; never make the state worse.
- Never hold `ELSPETH_JUDGE_METADATA_HMAC_KEY` in an agent shell. Check
  `env | grep ELSPETH_JUDGE` at the start and report it if present.
- Run `scripts/branch-safety-check.sh --intent commit` before every commit, and commit by
  pathspec — this is a shared checkout with other sessions staging into the same index.
- Choose tests by the reach of the change. Work item 2 touches composer redaction and
  session helpers, so run the affected suites plus any whole-tree gate whose scanned inputs
  could change; state the scope and limits of what you ran.

## When both are fixed

Each becomes an ordinary public GitHub issue describing work that is done, or simply a
changelog entry — that is the project's stated rule, and it is why they were held rather
than published. For the sentinel custody one the file is already written to house style at
`docs/github-issues/held/`; move it back to `docs/github-issues/issues/`, re-run
`python3 docs/github-issues/check_issues.py`, and import it with
`GH_TOKEN="$(gh auth token -u johnm-dta)" python3 docs/github-issues/import_issues.py --execute`
(the importer is resumable and skips what is already in `.import-state.jsonl`).

**One thing to be clear about:** the held files are already publicly readable. They are
tracked and have been on `origin/release/0.8.1` — a public repository — since they were
written. Holding them back withholds *amplification*, not disclosure. So do not treat "it
has not been imported yet" as containment; if the content itself is judged too sensitive to
sit in the tree, the tracked file has to be dealt with as well.
