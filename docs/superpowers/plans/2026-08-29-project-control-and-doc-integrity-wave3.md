# Project control and documentation integrity wave 3

**Status:** Reviewed and ready for execution
**Date:** 2026-08-29
**Baseline:** `9918fedb01f4b495dc8d1b7f8155935044b2db1a`
**Target branch:** `feature/unified-lineage`
**Working branch:** `docs/project-control-integrity-wave3`
**Worktree:** `.claude/worktrees/project-control-integrity-wave3`

## Objective

Deliver the first honest, public-safe Project Control Report (PCR) cycle
defined by ADR-024, repair systematic public-authority and provenance defects
in active ADRs, restore the live DAG documentation hub to current authority,
and remove a narrowly proven cohort of implemented or stale records from the
active documentation tree.

The end state remains lean: four compact project-control artifacts, no new
approval chain or project board, no invented T&M figures or forecast dates, and
no second source of truth for delivery detail already held by Filigree, Git,
CI, ADRs, or organisational financial systems.

## Authoritative inputs and constraints

- ADR-024 is authoritative for the reporting model, cadence, RAG semantics,
  source hierarchy, and public-information handling.
- Filigree is authoritative for current delivery scope, ownership,
  dependencies, status, and critical path.
- Git and CI evidence are authoritative for code and enforced-control state.
- Organisational time and financial systems are authoritative for T&M actuals,
  rates, approved resource envelopes, and tolerances. Those data are not
  available in this repository and must not be reconstructed.
- The public repository must contain no rates, invoices, personal timesheets,
  sensitive internal forecasts, private filesystem paths, or opaque private
  memory references presented as public evidence.
- Unknown information remains `Unknown` and creates a concrete source or
  authority action. `not set` is used only where the accountable authority has
  confirmed that no value exists. Neither state is rounded up to green or
  replaced with an invented date, owner, tolerance, or confidence percentage.
- The unrelated untracked file
  `docs/superpowers/plans/2026-08-29-composer-detail-level-wave1.review.json`
  in the primary checkout is user-owned and must remain untouched.

## Delivery sequence

Run every verification block with explicit fail-fast shell semantics (`set
-euo pipefail`); any failed assertion aborts the task rather than being masked
by a later successful command.

### Task 0: Commit the reviewed execution plan

Apply all accepted plan-review findings, then commit this plan before changing
the governed document set. This gives the plan real public Git history before
Task 5 retires it.

Sequence and verification:

```bash
set -euo pipefail
PLAN=docs/superpowers/plans/2026-08-29-project-control-and-doc-integrity-wave3.md
PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" .venv/bin/python -m pytest -q tests/unit/docs/test_archive_manifest_replacements.py
git add "$PLAN"
git commit -m "docs(plans): define project-control integrity wave 3"
git cat-file -e "HEAD:$PLAN"
test "$(git log -1 --format=%H -- "$PLAN")" = "$(git rev-parse HEAD)"
```

Commit:

```text
docs(plans): define project-control integrity wave 3
```

### Task 1: Establish the first sanitized PCR cycle

Create exactly four one-to-two-page living artifacts under
`docs/project-control/`:

1. `project-control-report.md`
2. `tm-register.md`
3. `raid-register.md`
4. `milestone-forecast-register.md`

Update `docs/README.md` so the set is findable. Add a prominent professional
pre-release banner near the top of the repository `README.md`: ELSPETH may be
suitable for carefully evaluated, use-case-specific adoption, but it is not
yet ready for general production use; readers must validate it against their
requirements and risk controls before relying on it.

All four repository artifacts are sanitized public derivatives of the named
authoritative source snapshots, not new sources of delivery, financial, or
assurance truth. The PCR is independently derived from the same cut-offs and
uses register IDs only as reconciliation anchors. Each artifact states its
publication class, report ID and as-of date, intended reader and task,
canonical source classes and cut-offs, authority boundary, review cadence, and
whether a controlled counterpart exists. If no controlled counterpart exists
at issue time, say so explicitly; do not imply that a future pack was the
source for the earlier public issue.

Before drafting, classify every candidate field and entry as `public`,
`controlled`, or `omitted`. A public entry may contain only a public-safe
summary and public-safe reference. Controlled detail is represented by a
neutral public ID and a statement that detail is retained in its controlled
source. Do not publish exploit detail, private links or endpoints, personal
contact data, contract/customer/procurement identifiers, or controlled system
names. The report ID is repository-local (`PCR-YYYY-NNN`) and embeds no
customer, contract, system, or personal identifier.

Mandatory value semantics:

- `Unknown`: authoritative evidence was unavailable or not checked;
- `not set`: the accountable authority confirmed that no value, owner, or date
  exists;
- `not established`: there is no evidence basis for a forecast or confidence
  judgement; and
- `Not applicable — first cycle`: prior-period comparison only.

Every non-Unknown material assertion, RAG component, owner, date, and
qualitative status carries a public-safe source, source as-of, and basis. A
controlled source is named by source class and cut-off, never by a private URL.
Claims that a control is currently enforced require fresh current-control
evidence; otherwise state the normative requirement or mark current state
Unknown.

Because the project has one assigned developer, this first public T&M register
contains no numeric time, cost, allocation, FTE, rate-derived, or productivity
figures. Missing values must not become zero, blank, a dash, or a derived
estimate. Future public aggregates require explicit public-information
classification from the authoritative organisational source.

The issued public report will use a stable report ID, an as-of date, and exact
source-data cut-offs. It will:

- state the reporting period or `Unknown`, plus the scope, resource, and
  milestone baselines available for the cycle;
- report outcome/scope, T&M, timing, risk/assurance, and overall status
  separately;
- identify this as the first reporting cycle, so there is no prior-period
  comparison;
- state current progress and the worst load-bearing constraints without using
  ticket counts as a proxy for outcome;
- identify the missing authoritative T&M extract, approved envelope, tolerance,
  and source owner as a control gap rather than inventing values;
- show the next load-bearing Filigree milestones and dependencies, applying
  the mandatory value semantics: commitments are `Unknown` when evidence is
  unavailable, `not set` only when the accountable authority confirms no
  commitment exists, and forecasts are `not established` where no evidence
  basis exists;
- carry only the live material RAID entries needed to understand delivery
  confidence and decisions;
- give each open ask a decision owner and decision-by date only where those are
  established; otherwise use `Unknown` when evidence is unavailable, use `not
  set` only when the accountable authority confirms no value exists, and name
  the action needed to establish the field; and
- label the repository copy as a sanitized derivative. A controlled issued
  pack, if one exists later, remains authoritative for sensitive T&M content.

The three register schemas remain those in ADR-024. The T&M register states the
unit or currency, actuals-through date, actual/accrual/forecast distinctions,
resource envelope and tolerance, allocation/reconciliation basis, and variance
reason—even when the honest value is Unknown. The RAID register carries stable
ID, type, consequence/exposure, posture, trend, status, last review, trigger
where useful, and an actionable owner/date or establishment action. The
milestone register carries intended outcome, owner, original and current
commitment, target, previous/current forecast range and confidence, critical
dependencies, status, period change, and any authorized rebaseline; target,
forecast, and commitment are never conflated. Every PCR ask includes the
decision or options, recommendation when supportable, decision owner,
decision-by date, consequence of delay, and escalation route, using the
mandatory value semantics above where authority has not established a field.

The evidence capture is reproducible and timestamped: record the Git head and
branch, Filigree critical path, selected issue snapshots, and source command
cut-offs used for the report. Do not commit raw controlled source exports; the
public artifacts retain only their public-safe claim/basis references.

Findability is contained within the four-artifact set:

- `docs/README.md` provides a Project control entry, with the PCR as the
  decision-maker entry point and direct links to the three registers;
- the PCR contains a compact companion-register table explaining and linking
  each register; and
- each register contains a reciprocal Return to PCR link.

Acceptance criteria, subject to the public-information classification above:

- for public entries, the PCR lets an authorized decision-maker identify status, exceptions,
  recommendations or options, open asks, consequence of delay, and next action
  without opening a register;
- for public entries, the T&M register lets a source owner or decision-maker identify exactly which
  evidence, envelope, or tolerance is absent and how to establish it;
- for public entries, the RAID register lets the maintainer identify exposure, trigger, posture,
  next action, owner, and due or review date without opening a tracker item for
  the governing facts; and
- for public entries, the milestone register lets a decision-maker distinguish target,
  commitment, and forecast, see dependencies and period change, and understand
  what prevents a forecast where none exists;
- for controlled or omitted entries, expose only the neutral ID, public-safe
  status or decision need, and `detail retained in controlled source`; the
  absence of a controlled counterpart never justifies publishing controlled
  detail;
- the PCR reconciles directly to the three registers by stable IDs;
- no unsupported number, date, owner, tolerance, rate, budget, or confidence
  appears;
- every live RAID entry has a next action or explicit monitoring posture;
- report and registers name their source cut-offs and review cadence;
- `docs/README.md` links the set without presenting it as a full project method;
- the repository `README.md` carries the pre-release/general-production banner;
- a semantic public-information review finds no unsupported or controlled
  claim; no separate unit test is created solely for this four-document pack.

Verification:

```bash
test "$(find docs/project-control -maxdepth 1 -type f | wc -l)" -eq 4
test "$(find docs/project-control -maxdepth 1 -type f | sort)" = "$(printf '%s\n' docs/project-control/milestone-forecast-register.md docs/project-control/project-control-report.md docs/project-control/raid-register.md docs/project-control/tm-register.md | sort)"
rg -n 'PCR-|Unknown|not set|not established|Sanitized' docs/project-control
if rg -n '/home/|/Users/|/tmp/|file://|invoice|hourly rate|personal time' docs/project-control; then exit 1; fi
sed -n '1,30p' README.md | rg -i 'pre-release'
sed -n '1,30p' README.md | rg -i 'not yet ready for general production use'
sed -n '1,30p' README.md | rg -i 'requirements.*risk controls'
git diff --check
```

Commit:

```text
docs(project-control): issue first sanitized PCR cycle
```

### Task 2: Repair ADR public authority and provenance

Apply the public-integrity repair to these active ADRs:

- `001-plugin-level-concurrency.md`
- `002-routing-copy-mode-limitation.md`
- `004-adr-explicit-sink-routing.md`
- `005-adr-declarative-dag-wiring.md`
- `006-layer-dependency-remediation.md`
- `007-pass-through-contract-propagation.md`
- `008-runtime-contract-cross-check.md`
- `009-pass-through-pathway-fusion.md`
- `010-declaration-trust-framework.md`
- `011-declared-output-fields-contract.md`
- `012-can-drop-rows-contract.md`
- `013-declared-required-fields-contract.md`
- `014-schema-config-mode-contract.md`
- `015-creates-tokens-contract.md`
- `019-two-axis-terminal-model.md`
- `023-custom-python-ci-analyzer.md`
- `024-delivery-governance-for-single-maintainer-mode.md`
- `025-multi-source-ingestion.md`
- `026-durable-token-scheduler.md`
- `029-journal-is-barrier-buffer-truth.md`
- `030-multi-worker-deployment-shape.md`
- `031-tutorial-is-a-fixed-script-canary.md`
- `032-validate-by-trust-domain.md`
- `033-deferred-intent-admission-contract.md`
- `036-textract-profile-bound-bucket.md`

Update `docs/architecture/adr/000-template.md` and add
`tests/unit/docs/test_adr_public_integrity.py`.

Rules for the repair apply to this audited 25-ADR cohort:

- the accountable decision authority is `ELSPETH maintainer`;
- agent, model, panel, or tool activity may be retained only as clearly
  non-authoritative analysis or review evidence where it materially explains
  the decision;
- do not claim an Architecture Review Board, architecture team, core
  maintainers, or another body that does not exist;
- align ADR-004 and ADR-005 status metadata with the accepted index state;
- remove private `/home/john/...`, ephemeral `/tmp/...`, untracked archive, and
  opaque project-memory references as public authority;
- replace a reference only with a tracked ADR, runbook, test, commit, or
  Filigree record that genuinely supports the claim; otherwise remove or
  qualify the unsupported claim rather than inventing provenance; and
- preserve the technical decision, consequences, alternatives, and honest
  historical context.

ADR-024's project-control amendment is explicitly in scope for this
documentation-cleanup sequence, so ADR-024 also receives a narrow current-claim
and public-information review as the 25th audit target.
Claims about staffing or external affiliation must be supported by fresh,
authorized public evidence or generalized to the minimum fact needed by the
decision. Normative controls are labelled as requirements, not represented as
currently enforced without fresh branch-protection, ruleset, release, or gate
evidence; the presence of a workflow file alone does not prove enforcement.

This is a current-tip public-authority cleanup, not historical erasure. Before
copying or editing any path/reference, classify it. Stale local evidence paths
may remain in Git history. Credentials, sensitive URLs, personal information,
or genuinely confidential material require stopping for a separate revocation,
rotation, and history-remediation decision and must not be copied into the
archive or described as safe public provenance.

The regression test is developed test-first. Before editing the ADRs, add the
test and run it to observe failures caused by the current defects. The test
must be narrow enough to reject public-integrity regressions without banning
legitimate technical mentions of AI systems or normal temporary-directory
behaviour.

Acceptance criteria:

- all affected ADRs name the real accountable authority;
- no ADR in the full active corpus uses an AI/tool/panel or fictional board as
  an authority field, including amendment-level authority metadata;
- no ADR in the full active corpus relies on a private absolute path,
  ephemeral evidence path, or opaque private memory as public provenance;
- ADR-004 and ADR-005 report `Accepted` consistently;
- ADR-024 contains no unsupported public staffing, affiliation, approval, or
  current-control claim;
- the template tells future authors to separate accountable authority from
  review evidence; and
- the focused test first fails for the expected baseline defects and then
  passes after the repairs.

Verification:

```bash
PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" .venv/bin/python -m pytest -q tests/unit/docs/test_adr_public_integrity.py
if rg -n '/home/john|/Users/|/tmp/|MEMORY\.md::|project memory|Architecture Review Board' docs/architecture/adr; then exit 1; fi
git diff --check
```

Commit:

```text
docs(adr): name accountable authority and public provenance
```

The Task 2 specification review must attest file by file that changes are
confined to authority, status, provenance, and the narrow ADR-024 claim audit.
It enumerates and justifies every technical-section hunk so the original
decisions, consequences, alternatives, and honest history are demonstrably
preserved.

### Task 3: Make the live DAG hub authoritative and archive dated assessments

Archive the complete dated assessment cohort, preserving source-relative paths:

- 13 files under `docs/architecture/dag/assessments/2026-07-15-1415/`
- 6 files under `docs/architecture/dag/assessments/2026-07-17-1739/`
- 5 files under `docs/architecture/dag/assessments/2026-07-18-0319/`

Archive the two completed bootstrap plans with the same wave:

- `docs/superpowers/plans/2026-07-15-dag-information-area.md`
- `docs/superpowers/plans/2026-07-17-dag-scenario-corpus.md`

Before deletion, freeze the exact 35-file original-source inventory for Tasks 3
and 4 as sorted repository-relative path plus SHA-256. Set the execution-local
value `ARCHIVE_WAVE=2026-08-29-project-control-integrity-wave3` and resolve
`PRIMARY_CHECKOUT` at execution time from `git worktree list --porcelain`.
Never commit its expanded absolute value. Require that the durable final
destination `${PRIMARY_CHECKOUT}/docs-archive/${ARCHIVE_WAVE}/` does not exist.
Populate a uniquely named sibling staging directory, copy source-relative paths
byte-for-byte, compare its exact 35-path/digest set, and atomically rename it to
the final destination before any source deletion. The destination is outside
the disposable worktree and is never a link target from public docs.

Repair these retained live surfaces:

- `docs/architecture/dag/README.md`
- `docs/architecture/dag/scenario-corpus/README.md`
- `docs/architecture/dag/assessment-framework.md`
- `tests/unit/architecture/test_dag_scenario_corpus_contract.py`
- `docs/README.md`

The live DAG README must identify
`docs/architecture/dag/scenario-corpus/v1/manifest.yaml` and its contract test
as authority for corpus inventory, evidence, and verdict inputs. Active
Filigree work is authoritative for delivery status, dependencies, ownership,
and next action. Timestamped source snapshots for `elspeth-ef29ef6ba4`,
`elspeth-cb1053fe46`, and `elspeth-be41d0ea25` support the status review, but
stale corpus counts in tracker prose are not copied into the hub. Dated
assessment scorecards are historical evidence, not the current verdict. The
retained unit test must validate live documentation links and the live corpus
contract without pinning a dated assessment directory.

The retained assessment framework must also define the lifecycle of future
snapshots: incorporate supported findings and evidence references into the live
manifest first; treat the dated point-in-time work product as temporary; then
retain it through Git history and optional maintainer-local archive rather than
making it current authority. Explain that the completeness framework defines
15 product-quality criteria while the live manifest records 11 executable
lifecycle cells; the current verdict is derived from those live sources, not a
parallel dated status manifest.

Develop the DAG contract change red-green: first change the test to require the
live-authority links and remove the dated-current contract, then observe it fail
against the old hub. Repair the hub/framework/corpus links and archive the
dated files only after that red result. Validate both target files and Markdown
fragments for the DAG README, scenario-corpus README, assessment framework, and
completeness criteria.

Acceptance criteria:

- all 24 dated assessment files and both completed plans are absent from the
  active tracked tree and present byte-identically in the ignored archive;
- the live DAG README contains no dated assessment presented as current;
- the scenario-corpus README and assessment framework direct public readers to
  Git history for historical snapshots, mention any optional maintainer-local
  archive only generically, and keep the live manifest authoritative;
- no retained live file links to a removed assessment path;
- corpus manifest/runtime semantics are unchanged; and
- the focused DAG contract test passes.

Verification:

```bash
set -euo pipefail
PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" .venv/bin/python -m pytest -q tests/unit/architecture/test_dag_scenario_corpus_contract.py
for source in docs/architecture/dag/assessments/2026-07-15-1415 docs/architecture/dag/assessments/2026-07-17-1739 docs/architecture/dag/assessments/2026-07-18-0319; do test ! -e "$source" || exit 1; done
git diff --check
```

Commit:

```text
docs(dag): make live corpus the current authority
```

### Task 4: Archive the narrow low-risk stale cohort

Archive these eight implemented design specifications:

- `docs/superpowers/specs/2026-07-12-guided-completion-layout-favicon-design.md`
- `docs/superpowers/specs/2026-07-24-out-of-box-example-packaging-design.md`
- `docs/superpowers/specs/2026-07-26-dag-corpus-wave-a-design.md`
- `docs/superpowers/specs/2026-07-28-aws-rds-immutable-trust-root-design.md`
- `docs/superpowers/specs/2026-08-01-execution-validation-pipeline-refactor-design.md`
- `docs/superpowers/specs/2026-08-01-llm-source-design.md`
- `docs/superpowers/specs/2026-08-26-blob-json-expand-design.md`
- `docs/superpowers/specs/2026-08-26-reference-join-design.md`

Archive the resolved residue ledger with them:

- `docs/elspeth-lints/touched-file-residue-ledger.md`

Use the same durable ignored archive root and byte-identity procedure as Task 3. Do not
expand this task to the broader medium-confidence Superpowers cohort. In
particular, retain the unfinished Composer assistant design and the live
universal Web plugin policy design.

Acceptance criteria:

- all nine active files are removed and preserved byte-identically;
- retained tracked files contain no live inbound links to the nine removed
  paths;
- no live operational or security doctrine is lost; and
- no unrelated Superpowers plan or specification changes.

Verification:

```bash
set -euo pipefail
for source in docs/superpowers/specs/2026-07-12-guided-completion-layout-favicon-design.md docs/superpowers/specs/2026-07-24-out-of-box-example-packaging-design.md docs/superpowers/specs/2026-07-26-dag-corpus-wave-a-design.md docs/superpowers/specs/2026-07-28-aws-rds-immutable-trust-root-design.md docs/superpowers/specs/2026-08-01-execution-validation-pipeline-refactor-design.md docs/superpowers/specs/2026-08-01-llm-source-design.md docs/superpowers/specs/2026-08-26-blob-json-expand-design.md docs/superpowers/specs/2026-08-26-reference-join-design.md docs/elspeth-lints/touched-file-residue-ledger.md; do test ! -e "$source" || exit 1; done
git diff --check
```

Commit:

```text
docs(archive): retire implemented specs and resolved ledger
```

### Task 5: Archive record and implementation-plan retirement

Create a `MANIFEST.md` in the durable primary-checkout archive stating:

- the exact source baseline;
- the three archived cohorts and counts;
- byte-for-byte preservation before active-tree deletion;
- Git history as public provenance;
- the local ignored archive as a convenience copy, not active authority; and
- the live replacements for the dated DAG assessments.

The manifest enumerates every archived source path and SHA-256, with a
disposition and live authority/replacement or `Git history only; no live
replacement required`. It records the original baseline as source for the 35
pre-existing files. Historical copies remain byte-identical even where a
relative link no longer resolves after relocation; the manifest records the
corresponding live target instead of rewriting historical bytes.

After Tasks 0–4 and their task-level reviews are complete, copy this plan into
the archive and remove it from the active tree. Its committed history remains
the public execution record; it does not remain as a completed plan in live
docs. Record its committed revision separately in the manifest and add its
digest as the 36th payload. This operation is idempotent: accept an existing
plan payload only if its digest matches, write the replacement manifest in a
sibling temporary file, validate it, and atomically rename it into place.
Verify exactly 36 payloads plus one manifest, including the final 36-path/digest
set, both in the durable archive and after the tracked deletion. Verify `git
log --all --full-history -- <plan-path>` shows both addition and deletion.

Commit:

```text
docs(plans): retire completed wave 3 execution plan
```

## Review gates

Before implementation, four independent reviewers assess:

1. requirements and scope fidelity;
2. public information/security handling;
3. document-set architecture and cross-reference integrity; and
4. testability, archive safety, and merge/cleanup correctness.

After each implementation task, run an independent specification-compliance
review followed by an independent quality review. Fix all Critical and
Important findings before continuing. Review Task 5 after its implementation,
then run the final inbound-reference checks and an integrated adversarial review
over the complete diff.

## Integrated verification

Focused checks:

```bash
set -euo pipefail
TARGET_PREMERGE="$(git rev-parse feature/unified-lineage)"
git cat-file -e "${TARGET_PREMERGE}^{commit}"
PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" .venv/bin/python -m pytest -q tests/unit/docs/test_adr_public_integrity.py
PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" .venv/bin/python -m pytest -q tests/unit/architecture/test_dag_scenario_corpus_contract.py
PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" .venv/bin/python -m pytest -q tests/unit/docs/test_archive_manifest_replacements.py
git diff --check "$TARGET_PREMERGE"..HEAD
```

Repository-wide documentation tests:

```bash
PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" .venv/bin/python -m pytest -q -n 0 tests/unit/docs/ tests/unit/architecture/test_dag_scenario_corpus_contract.py
```

CI-equivalent suite, in one coordinator-owned lane after confirming no other
broad test job is competing for the machine:

```bash
PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" .venv/bin/python -m pytest -n 12 tests/
```

Because this wave adds one Python test and modifies another but changes no
production code or external-input boundary, the trust-tier and Wardline gates
are not expected to add signal. The independent reviewer may require them if
the final diff expands beyond documentation and documentation-contract tests.

Before claiming completion, verify:

- both `elspeth.__file__` and `elspeth_lints.__file__` resolve inside the
  worktree before trusting any worktree test;
- the worktree is clean;
- the durable primary archive contains exactly 36 payloads plus one manifest,
  and payload hashes match the pre-deletion sources;
- the primary checkout still contains only its pre-existing unrelated
  untracked file;
- capture the final target pre-merge OID. If the target branch moved, compare
  every planned pre-existing affected path (35 deletions and 32 modifications)
  and both protected retained designs against the frozen baseline; stop and
  re-review any overlap. Also assert that the five additions remain absent on
  the target. For safe disjoint drift, merge the captured target into the work
  branch and rerun the complete verification set. Re-read the target tip
  immediately before final merge and repeat this check if it moved again;
- stage exact task pathspecs only, never `git add .`;
- the final tracked diff against the captured target OID matches the literal
  Appendix A allowlist exactly: 5 additions, 32 modifications, and 35 deletions
  (72 paths total). Aggregate counts alone are insufficient. The retained
  Composer-assistant and universal Web-plugin-policy designs are unchanged;
- after Task 5 removes the self-matching plan, guarded tracked-tree `git grep`
  checks find no occurrence of the three retired DAG assessment directory
  stamps, either retired bootstrap-plan path, or any of the nine retired
  low-risk basenames;
- the working branch is merged into `feature/unified-lineage` with `--no-ff`;
- the merge commit has two parents;
- `HEAD^1..HEAD` matches the same literal Appendix A allowlist;
- the focused, repository-documentation, and CI-equivalent checks pass again on
  the integrated target;
- the working tip is an ancestor of the integrated target;
- the unrelated primary file has the same checksum before and after; and
- the durable archive is reverified before the worktree is removed and the
  work branch is deleted with `git branch -d`. After cleanup, verify the
  archive and its digests again, confirm the worktree and branch are absent,
  confirm the integrated target HEAD, and confirm the protected primary-file
  checksum. On any failure, retain both.

The final no-inbound-reference check iterates the exact removed directory,
path, and basename strings and fails on any tracked match:

```bash
for retired in 2026-07-15-1415 2026-07-17-1739 2026-07-18-0319 2026-07-15-dag-information-area.md 2026-07-17-dag-scenario-corpus.md 2026-07-12-guided-completion-layout-favicon-design.md 2026-07-24-out-of-box-example-packaging-design.md 2026-07-26-dag-corpus-wave-a-design.md 2026-07-28-aws-rds-immutable-trust-root-design.md 2026-08-01-execution-validation-pipeline-refactor-design.md 2026-08-01-llm-source-design.md 2026-08-26-blob-json-expand-design.md 2026-08-26-reference-join-design.md touched-file-residue-ledger.md; do
  if git grep -n -F -- "$retired" -- .; then exit 1; fi
done
```

## Explicitly deferred

- the broader 48-file medium/high-confidence Superpowers archive cohort;
- release PDF source/distribution retirement or regeneration;
- a full PRINCE2/PID/business-case/stage-plan suite;
- new project boards, approval chains, decision registers, or signed document
  packages;
- invented T&M allocation, budget, rates, milestone dates, tolerances, owners,
  or confidence values; and
- mutation of Filigree issues or organisational financial records.

## Appendix A: exact final tracked-diff allowlist

The machine check extracts this Appendix A block from the durable byte-identical
plan payload (or from the plan's pre-deletion parent commit), sorts it into a
temporary expected file, sorts `git diff --name-status --no-renames` output
into a temporary actual file, and uses `cmp` on the two. It runs first for
`$TARGET_PREMERGE..HEAD` on the work branch and then for `HEAD^1..HEAD` after
the no-fast-forward integration, against these exact 72 entries:

```text
A	docs/project-control/milestone-forecast-register.md
A	docs/project-control/project-control-report.md
A	docs/project-control/raid-register.md
A	docs/project-control/tm-register.md
A	tests/unit/docs/test_adr_public_integrity.py
M	README.md
M	docs/README.md
M	docs/architecture/adr/000-template.md
M	docs/architecture/adr/001-plugin-level-concurrency.md
M	docs/architecture/adr/002-routing-copy-mode-limitation.md
M	docs/architecture/adr/004-adr-explicit-sink-routing.md
M	docs/architecture/adr/005-adr-declarative-dag-wiring.md
M	docs/architecture/adr/006-layer-dependency-remediation.md
M	docs/architecture/adr/007-pass-through-contract-propagation.md
M	docs/architecture/adr/008-runtime-contract-cross-check.md
M	docs/architecture/adr/009-pass-through-pathway-fusion.md
M	docs/architecture/adr/010-declaration-trust-framework.md
M	docs/architecture/adr/011-declared-output-fields-contract.md
M	docs/architecture/adr/012-can-drop-rows-contract.md
M	docs/architecture/adr/013-declared-required-fields-contract.md
M	docs/architecture/adr/014-schema-config-mode-contract.md
M	docs/architecture/adr/015-creates-tokens-contract.md
M	docs/architecture/adr/019-two-axis-terminal-model.md
M	docs/architecture/adr/023-custom-python-ci-analyzer.md
M	docs/architecture/adr/024-delivery-governance-for-single-maintainer-mode.md
M	docs/architecture/adr/025-multi-source-ingestion.md
M	docs/architecture/adr/026-durable-token-scheduler.md
M	docs/architecture/adr/029-journal-is-barrier-buffer-truth.md
M	docs/architecture/adr/030-multi-worker-deployment-shape.md
M	docs/architecture/adr/031-tutorial-is-a-fixed-script-canary.md
M	docs/architecture/adr/032-validate-by-trust-domain.md
M	docs/architecture/adr/033-deferred-intent-admission-contract.md
M	docs/architecture/adr/036-textract-profile-bound-bucket.md
M	docs/architecture/dag/README.md
M	docs/architecture/dag/assessment-framework.md
M	docs/architecture/dag/scenario-corpus/README.md
M	tests/unit/architecture/test_dag_scenario_corpus_contract.py
D	docs/architecture/dag/assessments/2026-07-15-1415/00-coordination.md
D	docs/architecture/dag/assessments/2026-07-15-1415/01-discovery-findings.md
D	docs/architecture/dag/assessments/2026-07-15-1415/02-capability-evidence.md
D	docs/architecture/dag/assessments/2026-07-15-1415/03-completeness-model.md
D	docs/architecture/dag/assessments/2026-07-15-1415/04-dag-completeness-gap-analysis.md
D	docs/architecture/dag/assessments/2026-07-15-1415/evidence/evidence-authoring-contracts.md
D	docs/architecture/dag/assessments/2026-07-15-1415/evidence/evidence-core-dag.md
D	docs/architecture/dag/assessments/2026-07-15-1415/evidence/evidence-runtime-recovery.md
D	docs/architecture/dag/assessments/2026-07-15-1415/evidence/validation-04-final-report.md
D	docs/architecture/dag/assessments/2026-07-15-1415/provenance/task-authoring-contracts.md
D	docs/architecture/dag/assessments/2026-07-15-1415/provenance/task-core-dag.md
D	docs/architecture/dag/assessments/2026-07-15-1415/provenance/task-runtime-recovery.md
D	docs/architecture/dag/assessments/2026-07-15-1415/provenance/task-validator.md
D	docs/architecture/dag/assessments/2026-07-17-1739/00-baseline-and-method.md
D	docs/architecture/dag/assessments/2026-07-17-1739/01-executed-evidence.md
D	docs/architecture/dag/assessments/2026-07-17-1739/02-scorecard-and-scenario-matrix.md
D	docs/architecture/dag/assessments/2026-07-17-1739/03-gap-analysis-and-remediation.md
D	docs/architecture/dag/assessments/2026-07-17-1739/evidence/tracker-snapshot.md
D	docs/architecture/dag/assessments/2026-07-17-1739/evidence/validation.md
D	docs/architecture/dag/assessments/2026-07-18-0319/00-baseline-and-method.md
D	docs/architecture/dag/assessments/2026-07-18-0319/01-executed-evidence.md
D	docs/architecture/dag/assessments/2026-07-18-0319/02-scorecard-and-scenario-delta.md
D	docs/architecture/dag/assessments/2026-07-18-0319/evidence/tracker-snapshot.md
D	docs/architecture/dag/assessments/2026-07-18-0319/evidence/validation.md
D	docs/elspeth-lints/touched-file-residue-ledger.md
D	docs/superpowers/plans/2026-07-15-dag-information-area.md
D	docs/superpowers/plans/2026-07-17-dag-scenario-corpus.md
D	docs/superpowers/specs/2026-07-12-guided-completion-layout-favicon-design.md
D	docs/superpowers/specs/2026-07-24-out-of-box-example-packaging-design.md
D	docs/superpowers/specs/2026-07-26-dag-corpus-wave-a-design.md
D	docs/superpowers/specs/2026-07-28-aws-rds-immutable-trust-root-design.md
D	docs/superpowers/specs/2026-08-01-execution-validation-pipeline-refactor-design.md
D	docs/superpowers/specs/2026-08-01-llm-source-design.md
D	docs/superpowers/specs/2026-08-26-blob-json-expand-design.md
D	docs/superpowers/specs/2026-08-26-reference-join-design.md
```

The two protected retained designs are outside this allowlist and must remain
byte-identical:

```text
docs/superpowers/specs/2026-07-10-composer-assistant-tools-design.md
docs/superpowers/specs/2026-07-12-universal-web-plugin-policy-design.md
```
