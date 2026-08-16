# State engine — local residual split (elspeth-efb47cb5fd, first action)

Date: 2026-08-17
Branch: `release/0.7.2` @ `2760f5184`
Owner issue: `elspeth-efb47cb5fd` (local-lane cells left unknown by assessment
`2026-08-15-0537`). Parent: `elspeth-1040aa2143`.
Predecessor: `docs/plans/2026-08-15-state-engine-six-issue-disposition.md` §3 step 3,
which said the first action on this issue is to split PB-09's 2,022 local cells into
locally-authorable vs provider-dependent "before committing effort".

> **STATUS 2026-08-17: DECIDED.** John ruled Decision 1 = (1a) and Decision 2 =
> (2b) then (2a). Both rulings are written into
> `docs/architecture/state_engine/completeness-criteria.md` as normative
> amendments ("Follower-profile applicability is narrowed per leg, in the
> catalog" and "Provider-backed plugins on the SQLite profiles"). Owned work:
> the 2b per-leg table + catalog `cell_applicability` amendment, the 1a
> contract-fake PB-09 authoring, and the 2a follower-composition build are filed
> as tasks under `elspeth-efb47cb5fd` (see its comments for IDs). §3–§4 below are
> the decision record; §5 (Tranche 1) is unaffected and proceeds.

Every number below is recomputed from the machine record —
`proof-catalog/v3/evidence_selectors.json` (cell → lane) minus
`assessments/2026-08-15-0537/assessment.json` overrides — and reproduces the
published totals exactly (4,741 local / 1,780 live; PB-09 2,022 / 720).

## 1. The premise in the predecessor plan was the wrong lens

The predecessor treated PB-09's provider-backed plugins as the sizing question. They
are not the mass. **The two SQLite follower profiles are.**

| Profile | Local residual | Share |
|---|---:|---:|
| `sqlite-wal-web-hosted-leader-plus-same-host-cli-followers` | 1,740 | 36.7% |
| `sqlite-wal-same-host-leader-plus-claim-only-followers` | 1,711 | 36.1% |
| `sqlite-wal-single-process-leader` | 1,290 | 27.2% |

For **68 of the 73 legs** the two follower profiles are unmapped on all ten
dimensions (10 + 10 cells per leg). The follower lanes hold 30 and 3 nodes total,
and those nodes are follower-*specific* harnesses (join/drain/eviction/chaos,
`test_follower_*`, `test_state_engine_lifecycle_profiles`). Nothing re-proves the
other legs' contracts *under* a leader+follower deployment.

The single-process lane, by contrast, is a mapping-saturated lane: 601 of 712 nodes
are bound to cells and the 111 unclaimed carry recorded exclusion reasons. Its
residual is an **authoring** gap, concentrated in two dimensions that are near-absent
everywhere: `concurrency` (7 bound nodes) and `maintenance` (6).

## 2. The split — four tiers by which decision they wait on

Provider-backed = the 38 plugin variants the reviewed matrix
(`tests/golden/state_engine/plugin_lifecycle_matrix.json`) marks
`external_observation_required: true` / `local_fixture: provider-contract-fake`.
The other 34 are `hermetic` (32) or `real-process-http` (2: `blob_fetch`, `web_scrape`).

| Tier | Cells | What it is | Waits on |
|---|---:|---|---|
| **T1** | **910** | First-party legs and plugins, single-process profile: the unmapped dimensions. | nothing — authorable now |
| **D1** | 380 | Provider-backed PB-09 plugins, single-process profile, all 10 dims (38 × 10). | Decision 1 |
| **D2** | 2,691 | First-party legs/plugins on the two follower profiles. | Decision 2 |
| **D1+D2** | 760 | Provider-backed PB-09 plugins on the two follower profiles (38 × 20). | both |
| | **4,741** | | |

So: 910 cells are unambiguous; 3,831 (81%) hang on one or both of the decisions
below. The predecessor's "PB-09 provider split" is the 1,140 = D1 + D1+D2.

T1 by dimension: maintenance 133 · concurrency 130 · read_model_truth_table 114 ·
guard_refusal 96 · zero_mutation_rollback 95 · precondition_image 84 ·
crash_restart 81 · boundary_composition 77 · production_entry 66 · success_effects 34.
T1 by family: PB 488 (PB-09 first-party 205, PB-06 ~153, PB-10 60, PB-07 40, others) ·
TS 134 · F 109 · RM 90 · AUX 55 · RC 34.
Worklist: one (leg, dimension, case) triple per cell, generated from the record.

## 3. Decision 1 — provider-contract-fake on the SQLite profiles (1,140 cells)

The facts in tension:

- The catalog marks all ten PB-09 dimensions **required** for every provider-backed
  variant on all three SQLite profiles, and `evidence_selectors.json` places those
  cells in the **local** lanes.
- The protected `live_provider` lanes carry **only** the
  `postgresql-16-aws-single-leader-landscape` profile (10 cells per plugin). There
  is no lane, live or local, that could ever produce a provider plugin's SQLite-profile
  cells except a local run — and a local run has no provider.
- The reviewed matrix names each provider plugin's local fixture:
  `provider-contract-fake`. The lifecycle-matrix test's docstring says the opposite:
  "Provider-backed subjects are deliberately absent from this local pass set … remain
  unknown until those [live] lanes execute."
- `completeness-criteria.md` says `boundary_composition` needs the *real supported
  plugin … and external-effect boundaries* and the production_boundary
  `success_effects` acceptance names *external-effect images*. It says nothing about
  provider fakes either way. The review cycle demoted a *stand-in subject*
  (RM production_entry) — a fake standing in for the thing being proven.

Options:

- **(1a)** Sanction the contract fake at the **SDK-client seam** (boto3 client, Azure
  SDK client, Dataverse HTTP, chroma client, LLM provider client) as local evidence
  for all ten PB-09 dimensions on the SQLite profiles. Rationale: PB-09's contract is
  *lifecycle ordering* (fresh/resume/partial-start/teardown) between the real plugin
  code and the engine; the provider is the dependency, not the subject; the plugin's
  "external-effect image" for PB-09 is the ordered call sequence it emitted, which the
  fake captures exactly. Real external observation stays where the matrix puts it:
  the plugin's release lane on the AWS profile (and PB-06/07 for sink effects).
  **Requires a written amendment** to `completeness-criteria.md` / the v3 catalog
  README naming the fixture class and the seam — otherwise it is promotion by
  convenience, the F1 move in reverse.
- **(1b)** Sanction the fake for the eight engine-side dimensions only; leave
  `success_effects` and `boundary_composition` for provider plugins on SQLite
  profiles as permanent `unknown`. Honest but leaves 228 cells structurally unprovable
  and HG-09 open forever — i.e. the verdict can never be `complete`, which contradicts
  the catalog marking them required.
- **(1c)** Catalog amendment: provider-backed variants' SQLite-profile PB-09 cells
  become `not_applicable` with reason "provider plugins are proven on their release
  lane's profile"; the AWS-profile live cells remain the sole evidence. Removes 1,140
  cells from the residual by ruling, not by evidence.

Recommendation: **(1a)**, because it is the only option under which every required
cell has an evidence path *and* the evidence exercises the real plugin. The
amendment must state the seam precisely (the fake replaces the provider's client
object, never the plugin's own methods) so the reviewer can reject a mock-the-subject
test on sight. (1c) is legitimate if you would rather not fund 1,140 cells of fake
authoring, but it is a catalog ruling and yours to make.

## 4. Decision 2 — the follower-profile cross-product (3,451 cells)

`completeness-criteria.md`: "Every mandatory v2 cell is proved independently for
these catalog-owned state-store/deployment pairs … This is an evidence cross-product,
not an assertion that every lifecycle mode applies to every deployment. The catalog
lists the modes supported by each profile case." The v3 catalog already narrows some
legs by profile (PB-11 SQLite-N/A; RC-05/PB-08 follower-only). For the other 68 legs
it marks both follower profiles required on all ten dimensions.

Options:

- **(2a)** Build it. One profile-parametrized fixture family — "leader plus
  claim-only follower" and "web-hosted leader plus CLI follower", both already
  demonstrated in `tests/e2e/recovery/test_sink_effect_deployment_profiles.py`
  (`_observe_profile`, `spawn_database_process_at_seam`, `_run_cli_follower_*`) —
  and re-run each leg's proof under it with the reporter probe bound per node.
  Mechanical for many legs (same assertion, different deployment) but every node
  needs a real second process, a real profile probe, and one leg/case/profile
  subject. Order of thousands of new nodes; multi-week agent campaign; slow lanes.
- **(2b)** Catalog amendment narrowing follower-profile applicability per leg, with a
  written reason per (leg, profile): required where a second claimant can change the
  outcome (all lease/claim/coordination legs, follower lifecycle, RM read models,
  sink handoff, PB-06/07/08/09/10), `not_applicable` where the follower profile is
  provably not on the path (e.g. transform-internal contracts, forbidden-state pins,
  AUX invariants that hold per-connection). Then build (2a) for what remains.
- **(2c)** Leave as is: 3,451 cells stay `unknown`, HG-09 stays open even after AWS,
  verdict never `complete`.

Recommendation: **(2b) then (2a)** — the criteria explicitly give the catalog the
authority to say which modes apply to which profile, and a per-leg reasoned table is
the honest instrument (not a blanket N/A). Even a conservative narrowing leaves a
large (2a) build; the table just makes it the *right* build. This is a catalog
change and yours to rule on; I can draft the per-leg table for review.

## 5. Tranche 1 (910 cells) — authorable now, regardless of decisions

Scope: first-party legs and plugins, `sqlite-wal-single-process-leader`, the unmapped
dimensions. Two sub-tranches by mechanism:

- **T1-a `maintenance` (133) and `concurrency` (130)** — systematically absent, so
  they need a *pattern*, not per-leg improvisation:
  - `maintenance` = "exact evidence locators remain collected and run in the
    maintained verification selection, with coherent actionable gap themes either
    live-owned in Filigree or explicitly unowned" (forbidden family: "the
    deliberate-absence decision remains maintained"). Candidate: one parametrized
    node per leg asserting the leg's selector locators are collected by the
    maintained selection and its owner/gap-theme is live — bound to the lane's
    profile like the existing six. Must be checked against the criteria's ban on
    source-inspection promoting a *behavioral* case; maintenance is the one
    dimension whose acceptance text is about selection, so it may be legitimately
    non-behavioral. Resolve before authoring 133 of them.
  - `concurrency` = "independent connections/processes prove winner, loser,
    ordering, and ABA/generation behavior". The 7 existing nodes show the honest
    shape: two Tier-1 engines, or two spawned OS processes
    (`test_registered_process_authority.py`). Single-process profile permits
    independent *connections*; where the contract is process-scoped, spawned
    processes. Per-leg authoring against the leg's contract text.
- **T1-b the other eight dimensions (647)** — per-leg mapping-and-authoring in the
  style of the campaign's 9 mapping agents: read the leg's contract + family
  acceptance text, find or write the assertion, one leg/case/profile subject per
  node, bind to the lane. PB-09 first-party single-process (205: 6 dims × 34) is a
  natural first slice because the lifecycle harness already exists
  (`test_state_engine_plugin_lifecycle_matrix.py`) and only needs the six engine-side
  phases added per plugin.

Discipline (from the 08-15 review cycle, which demoted 7 cells for these failures):
no tautological passes, no stand-in subjects, no non-discriminating success effects,
no source-inspection promoting a behavioral case. A mapping that does not
discriminate is worse than an honest `unknown`. Every new node lands in the
maintained selection and `evidence_selectors.json`; promotion happens only through a
new (delta) assessment at a new freeze — the 2026-08-15-0537 package is
digest-frozen and is not edited.

## 6. What this buys — restated honestly

Nothing here moves the verdict. Exactly two legs (RC-05, PB-08) have zero live cells,
in two different cohorts. T1 + D1 + D2 complete = 4,741 residual cells retired,
verdict still `not_complete` behind the 1,780 live cells and HG-09. Fund it as
pre-payment so that AWS restoration is one dispatch away, not as verdict progress.
