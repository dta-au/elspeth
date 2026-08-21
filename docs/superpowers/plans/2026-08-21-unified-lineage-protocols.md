# Unified Lineage Campaign Protocols Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and execute the cross-workstream protocols every other campaign plan
references: the pre-flip fixture-freeze protocol and its WS1-checkpoint diff, the WS2
oracle-versioning/retirement procedure, the ruling-casualty migration worklist, the
per-slice whole-tree gate checklist, judge-bundle sequencing, and the WS1 STOP
procedure.

**Architecture:** A small oracle-freeze registry module plus a snapshot writer/compare
pytest suite turn spec §11's frozen-oracle protocol into enforced code: pre-flip stable
projections are committed as plain bytes, the (rewritten) harness is forever checked
against stored bytes rather than its own regeneration, and fixture retirement is
fail-closed on an adjudicated migration record. Everything procedural (gate checklist,
judge sequencing, STOP rule) lives in the Standing Procedures section below, referenced
by section id from sibling plans.

**Tech Stack:** Python 3.12+, pytest, the existing `tests/fixtures/dag_scenario_corpus`
harness (`run_scenario_case`, `StableRunProjection`), git on the shared `release/0.7.2`
checkout.

**Spec:** docs/superpowers/specs/2026-08-21-barrier-scopes-full-nesting-spec.md
(rev 3.2 — rulings 1–28 final; §11 is the frozen-oracle authority). Scout inputs (READ
them, they carry the verified line numbers and classifications):
docs/superpowers/plans/2026-08-21-unified-lineage-inputs/consumer-roster.md,
…/fixture-oracle.md, …/test-harness.md.

## Global Constraints

- Shared checkout, stage-by-pathspec-only discipline: `git add <explicit file paths>` —
  never `git add -A`, `-u`, or a directory. Commit only YOUR hunks; a sibling can sweep
  your staged files.
- Never bypass hooks, except the documented `--no-verify`-with-end-of-slice-reconciliation
  grant; `git stash` is blocked by hook.
- Full `pytest tests/` at slice boundaries — whole-tree AST gates miss scoped runs.
  Record `git rev-parse HEAD` before and after every long run; if HEAD moved, the
  result is uninterpretable — re-run.
- Trust-tier corpus diff before/after each slice, add NOTHING; COUNT findings, never
  `tail` them (§S2 step 2 is the procedure of record).
- Wardline gate (verbatim from AGENTS.md):
  `wardline scan . --fail-on ERROR --fail-on-inert --trust-pack scripts.wardline_pack --allow-custom-packs --local-only`
- No hand-edited judge signatures, ever; no judge-bundle staging across the campaign
  (§S4).
- Runtime-rejection parity: every new rejection site needs an adjudicated disposition
  in `config/cicd/runtime_rejection_parity.yaml` plus its Stage-1 composer mirror
  (`scripts/cicd/runtime_rejection_parity.py --write`, then adjudicate — never hand-edit
  a `key`).
- Depth cap and fixpoint bound (campaign-wide): the supported guarantee is 5 layers of
  bound-region nesting, builder-enforced fail-closed, config-overridable; the
  escalation fixpoint's non-convergence bound is derived at build from the actual depth
  (+ margin), never a constant.
- Do NOT edit or stage `src/elspeth/web/composer/state.py` or
  `tests/unit/web/composer/test_state.py` — the maintainer is committing them.
- Read docs/agents/recent-code-hints.md before writing code.
- Standing procedures: docs/superpowers/plans/2026-08-21-unified-lineage-protocols.md
  §S1–§S5 govern fixture freezing, slice gates, casualty retirement, judge-bundle
  sequencing, and the WS1 STOP rule. This plan is the source of record for those
  procedures; every sibling campaign plan carries this same citation line.

---

## Standing Procedures (referenced by every campaign plan as "protocols plan §S_n_")

### §S1 — Fixture-freeze protocol (execute before the WS1b flip — WS1b Task 7) and the WS1-checkpoint diff

**Ordering, fail-closed** ("pre-flip" throughout this plan = after WS1a Task 8a has
landed, before the WS1b flip commit — WS1b Tasks 7–12 — begins; WS1a's slices are
behaviour-neutral prep, so the stable projections are unchanged through them):

1. WS0 lands (docs/superpowers/plans/2026-08-21-unified-lineage-ws0-corrections.md).
2. The new NESTED differential fixtures land and are classified FROZEN (fixture-oracle
   scout Risk 2: `sequential-nested-fork-coalesce` EXISTS but is sequential — two
   depth-1 regions in series; §4.1a rows 2–4 and depth 2+ have ZERO substrate today).
   Owned by **WS1a Task 8a** (fork-in-fork depth-2 and expand-in-fork scenario dirs
   under `tests/fixtures/dag_scenario_corpus/v1/`, wired into `EXPECTED_SCENARIOS` and
   this plan's `SCENARIO_CLASSIFICATION` as FROZEN); the freeze is NOT authoritative
   until they exist, because a fixture created after the rewrite cannot be its own
   oracle.
3. Task 3 below runs the snapshot writer at a clean, recorded pre-flip HEAD and commits
   the bytes under `tests/fixtures/dag_scenario_corpus/oracle_freeze/v1/`.

**The oracle is the stored bytes, not the harness.** `harness.py::_stable_projection`
reads the retired tri-columns at four sites (`:925/:933/:944/:951`, `:971`, `:1111`,
`:1134-1136`, `:1146`) and is itself rewritten in WS1 — a rebuilt harness verifying its
own rebuild is the self-oracle shape §11 forbids. The compare mode of
`tests/integration/core/dag/test_oracle_freeze.py` closes that hole permanently.

**WS1-checkpoint diff procedure** (run at the end of WS1, before WS2 starts):

```bash
git rev-parse HEAD                         # record; working tree must be clean
.venv/bin/pytest tests/integration/core/dag/test_oracle_freeze.py -q
# FROZEN scenarios: byte-identical or the checkpoint FAILS.
# REGENERATED_WS1 (row-union-interleave): invariant subset (sink bytes +
# disposition multiset) enforced automatically; adjudicate the full delta by hand:
ELSPETH_ORACLE_FREEZE=write .venv/bin/pytest \
  "tests/integration/core/dag/test_oracle_freeze.py" -q -k "row-union-interleave"
git diff tests/fixtures/dag_scenario_corpus/oracle_freeze/v1/row-union-interleave/
# Review: the ONLY deltas may be branch_name disappearing from released tokens
# (ruling 27) plus the consequent token-key ordinal reshuffle cascading through
# keyed records (fixture-oracle scout Risk 7 — a whole-projection delta, NOT a
# one-field diff). Then RESTORE the pre-flip bytes — the snapshot is the permanent
# pre-rewrite record and is never rewritten:
git checkout -- tests/fixtures/dag_scenario_corpus/oracle_freeze/
```

Manifest `projection_sha256` pins churn at WS1 for regenerated surfaces and for any
export-surface change; those rotations are hand-adjudicated per the existing ledger
discipline in `tests/unit/architecture/test_dag_scenario_corpus_contract.py` (dated A/B
notes + `EXPECTED_CASE_REGISTRY_SHA256`), and the WS1b plan owes the one-shot
whole-corpus diff tool (test-harness scout Risk 7). The freeze surface built here
deliberately EXCLUDES `audit_records` and manifest blobs, so new audit tables
(`token_lineage_frames`, `group_records`, `group_losses`) never churn the frozen bytes
(fixture-oracle scout Risk 4). Ruled: the three new tables enter the portable export
AT the WS1b flip; the resulting `projection_sha256`/`audit_record_counts` manifest
churn is adjudicated in WS1b's manifest-rotation slice, and the freeze surface here is
immune by construction.

### §S2 — Whole-tree gate checklist, run at EVERY slice boundary of EVERY workstream

Run all steps; a slice is not closed until all pass.

1. **Full suite, HEAD-fenced** (~18 min; a red run with a moved HEAD is
   uninterpretable — re-run, never diagnose):

   ```bash
   git rev-parse HEAD | tee /tmp/claude-1000/slice-head-before
   .venv/bin/pytest tests/ -n 12
   git rev-parse HEAD | tee /tmp/claude-1000/slice-head-after
   diff /tmp/claude-1000/slice-head-before /tmp/claude-1000/slice-head-after
   ```

2. **Trust-tier corpus diff — add nothing.** The gate exits 1 with a large corpus BY
   DESIGN (fail-closed signing state, `elspeth-13f0cc04fb`); the baseline is the
   corpus, not zero. The baseline must come from a FULL clean-tree export (a dirty
   working tree or partial export lies):

   ```bash
   SCRATCH=/tmp/claude-1000/tt-baseline; rm -rf "$SCRATCH"; mkdir -p "$SCRATCH"
   git archive HEAD | tar -x -C "$SCRATCH"          # BEFORE the slice's commits: use the pre-slice HEAD
   (cd "$SCRATCH" && ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
     /home/john/elspeth/.venv/bin/elspeth-lints check --rules all --root src/elspeth \
     > /tmp/claude-1000/tt-before.txt); true
   # after the slice's commits (clean tree at the new HEAD):
   ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
     elspeth-lints check --rules all --root src/elspeth > /tmp/claude-1000/tt-after.txt; true
   # COUNT via the tool's own reported total / by grepping the finding-line pattern
   # observed in the output — NEVER `tail` the stream:
   diff <(sort /tmp/claude-1000/tt-before.txt) <(sort /tmp/claude-1000/tt-after.txt)
   ```

   Acceptance: identical counts and identities, OR a diff consisting solely of findings
   the slice deliberately REMOVED. Any added finding blocks the slice. Never shape code
   around signature churn to dodge this; binding churn is an honest release obligation.

3. **Wardline gate** (exit 0 = clean AND non-inert; 1 = findings or inert; 2 = grant or
   config error):

   ```bash
   wardline scan . --fail-on ERROR --fail-on-inert \
     --trust-pack scripts.wardline_pack --allow-custom-packs --local-only
   ```

4. **Runtime-rejection parity** (mandatory whenever the slice touched
   `src/elspeth/core/dag/` or `src/elspeth/core/config.py`; cheap enough to run always):

   ```bash
   .venv/bin/python scripts/cicd/runtime_rejection_parity.py --write
   git diff --stat config/cicd/runtime_rejection_parity.yaml
   ```

   Empty diff expected unless the slice added/rekeyed a rejection site — then adjudicate
   the seeded entry (mirrored/abstains/structural/not_authorable/unmirrored-under-ratchet)
   and commit it WITH the slice. Never hand-edit a `key`.

5. **Composer three-pin** (mandatory whenever the slice touched SourceSpec/NodeSpec/
   OutputSpec, a composer tool argument model, or the `collectors:`/`scopes:` config
   surface — WS2 especially):

   ```bash
   .venv/bin/pytest tests/unit/web/composer -k "capability_skill_identity" -q
   .venv/bin/python scripts/cicd/bootstrap_redaction_snapshot.py --write
   git diff tests/unit/web/composer/redaction_policy_snapshot.json   # only hashes may move,
                                                                     # never sensitive_path_count, unless intended
   grep -n "exactRecord" src/elspeth/web/frontend/src/api/guidedDecoder.ts
   ```

   The `guidedDecoder.ts` `exactRecord` key lists reject unenumerated keys AT RUNTIME
   and are invisible to every backend suite — check them by eye against the fields the
   slice added.

6. **Serialisation hash pins:**

   ```bash
   .venv/bin/pytest tests/unit/web/composer/test_state_serialisation_contract.py -q
   ```

   A reddened hash pin is the gate WORKING — decide what happens to already-persisted
   states before re-pinning (new optional spec fields serialize omitted-when-None; the
   collector binding key in particular, spec §3).

7. **Oracle freeze compare** (from Task 2 of this plan, once it exists):

   ```bash
   .venv/bin/pytest tests/integration/core/dag/test_oracle_freeze.py -q
   ```

### §S3 — Oracle versioning at WS2: how a ruling-casualty fixture LEAVES the frozen set

A fixture whose topology is rejected by the new §7 rules stays FROZEN through the WS1
diff, then leaves at WS2 with an adjudicated migration — so migration is always
distinguishable from tampering. The retirement of scenario `<S>` is ONE commit
containing ALL of:

1. The replacement fixture(s) (if any) added under `v1/` and classified in
   `SCENARIO_CLASSIFICATION` (Task 1's table).
2. Manifest surgery: `<S>`'s cases removed/replaced in
   `docs/architecture/dag/scenario-corpus/v1/manifest.yaml`;
   `EXPECTED_SCENARIOS` in `tests/fixtures/dag_scenario_corpus/schema.py` updated if the
   scenario name itself changes.
3. A dated adjudication note appended to the rotation ledger in
   `tests/unit/architecture/test_dag_scenario_corpus_contract.py` and
   `EXPECTED_CASE_REGISTRY_SHA256` rotated there — the note names the ruling
   (23/25/28/§7-rule-4), the replacement, and the A/B evidence.
4. The snapshot directory moved, never deleted:
   `git mv tests/fixtures/dag_scenario_corpus/oracle_freeze/v1/<S> tests/fixtures/dag_scenario_corpus/oracle_freeze/retired/<S>`
   plus a new `oracle_freeze/retired/<S>/MIGRATION.md` recording ruling, date,
   adjudicator, replacement scenario, and the manifest-rotation commit subject.
5. `tests/unit/architecture/test_oracle_freeze_registry.py` stays green — it fails any
   classification entry that left `EXPECTED_SCENARIOS` without a `MIGRATION.md`
   (Task 1), which is the fail-closed half of this procedure.

### §S4 — Judge-bundle sequencing

- **Do not stage a judge-signature bundle at any point during this campaign.** Bundles
  are exact-source-bound; WS1/WS3 rewrite Tier-1-dense files, so any staged bundle is
  invalidated by the next slice. 0.7.2 allowlist signing sequences AFTER the campaign
  settles (spec §11 whole-tree gate obligations); the operator signs once, at package
  completion, after churn has settled.
- At campaign start, verify nothing is currently staged (record the answer in the
  campaign log): call `mcp__elspeth-judge__stage_status`; if a bundle IS staged,
  surface to the maintainer before WS1 — do not silently let campaign churn invalidate
  operator-visible staging.
- Fix tier-model defects as you find them; never make the tier-model state worse; never
  hand-edit a `judge_metadata_signature`; agents never hold
  `ELSPETH_JUDGE_METADATA_HMAC_KEY` ([O1]).

### §S5 — WS1 checkpoint and STOP procedure (spec §11, verbatim)

> **The checkpoint is end of WS1 (rev 3.1 — restated per ruling 26):** full suite green
> with observable deltas ONLY in the §4.1a-enumerated lineage-metadata surfaces and the
> new audit rows (`token_lineage_frames`, universal `group_records`). The FROZEN
> invariants, pinned by oracle before WS1 starts: plugin-visible outputs, routing
> decisions, dispatch arms, terminal dispositions, sink-effect identities, canonical
> hashes (§3 corpus), and the `dag_scenario_corpus` group-id-normalized stable
> projections (`tests/fixtures/dag_scenario_corpus/schema.py` —
> `StableTokenProjection`, `SinkOutputProjection`, `StableTerminalDisposition`,
> `StableExpansionProjection`), which are byte-stable across the rewrite BY CONSTRUCTION
> and are diffed pre/post as the delta oracle. Every fixture surface is classified
> **frozen** (behaviour-bearing: projections, dispositions, sink bytes, ordinals,
> branch names, the 56 golden JSONs each individually adjudicated) or **regenerated**
> (representation-bearing: raw token/journal rows) BEFORE WS1 starts — a rebuilt
> fixture is never the oracle for its own rebuild. **The oracle set is versioned per
> workstream:** fixtures whose topology is rejected by rulings 23/25 (e.g. the
> `parallel-coalesces` corpus fixture — two coalesces over one fork, which rule 2
> rejects; `fork-multiple-terminals-partial-failure` is PURE fan-out, fully unbound,
> and stays legal/frozen — the rev 3.1 text misnamed it) stay frozen
> through the WS1 diff, then leave the frozen set at WS2 with an adjudicated migration
> recorded — so migrating a ruling casualty is distinguishable from tampering with the
> oracle. If WS1 cannot reach green-with-only-enumerated-deltas, STOP and surface to
> the maintainer — do not press into WS3 on a red foundation.

Operationally: on a failed checkpoint, (1) do not start WS3/WS4 work, (2) do not
regenerate any FROZEN snapshot to make the diff pass, (3) write up the exact
non-enumerated delta (scenario, case, field, before/after) and hand it to the
maintainer. Abort evaluation happens at slice boundaries per the WS1 landing shape
(behaviour-neutral prep slices → ONE atomic representation flip → cleanup); any landed
state must leave the tree consistent, single representation per surface, no dual reads.

---

### Task 1: Oracle-freeze registry module + registry pins

**Files:**
- Create: `tests/fixtures/dag_scenario_corpus/oracle_freeze.py`
- Test: `tests/unit/architecture/test_oracle_freeze_registry.py`

**Interfaces:**
- Consumes: `tests/fixtures/dag_scenario_corpus/schema.py` —
  `ScenarioRunEvidence` (fields `scenario_id: str`, `case_id: str`,
  `runtime: RuntimeEvidence`), `RuntimeEvidence` (fields `status: str | None`,
  `rows_processed/rows_succeeded/rows_failed: int`,
  `sink_outputs: tuple[SinkOutputProjection, ...]`,
  `durable_projection: StableRunProjection | None`), `EXPECTED_SCENARIOS:
  tuple[tuple[str, str], ...]` (15 `(name, title)` pairs, `schema.py:75`).
- Produces (consumed by Task 2 and by the WS1/WS2 plans):
  - `class OracleClass(enum.Enum)` — `FROZEN | REGENERATED_WS1 | RULING_CASUALTY_WS2 | CONTESTED`
  - `SCENARIO_CLASSIFICATION: dict[str, OracleClass]`
  - `FREEZE_ROOT: Path` (= `tests/fixtures/dag_scenario_corpus/oracle_freeze/v1`)
  - `RETIRED_ROOT: Path` (= `…/oracle_freeze/retired`)
  - `frozen_surface(evidence: ScenarioRunEvidence) -> dict[str, object]`
  - `canonical_bytes(surface: dict[str, object]) -> bytes`
  - `snapshot_path(scenario_id: str, case_id: str) -> Path`
  - `invariant_subset(surface: Mapping[str, object]) -> dict[str, object]`

- [ ] **Step 1: Write the failing registry tests**

Create `tests/unit/architecture/test_oracle_freeze_registry.py`:

```python
"""Registry pins for the unified-lineage frozen-oracle protocol (spec §11).

The classification table is the per-workstream oracle versioning authority:
FROZEN fixtures must diff byte-identical across WS1; REGENERATED_WS1 keeps an
always-frozen invariant subset; RULING_CASUALTY_WS2 leaves the frozen set at
WS2 only with an adjudicated MIGRATION.md; CONTESTED blocks the freeze until
the maintainer rules.
"""

import json

from tests.fixtures.dag_scenario_corpus.oracle_freeze import (
    RETIRED_ROOT,
    SCENARIO_CLASSIFICATION,
    OracleClass,
    canonical_bytes,
    invariant_subset,
)
from tests.fixtures.dag_scenario_corpus.schema import EXPECTED_SCENARIOS


def test_every_expected_scenario_is_classified_and_orphans_carry_migration_records() -> None:
    expected = {name for name, _title in EXPECTED_SCENARIOS}
    classified = set(SCENARIO_CLASSIFICATION)
    assert expected <= classified, f"unclassified scenarios: {sorted(expected - classified)}"
    for orphan in sorted(classified - expected):
        record = RETIRED_ROOT / orphan / "MIGRATION.md"
        assert record.exists(), (
            f"Scenario {orphan!r} left EXPECTED_SCENARIOS without an adjudicated "
            f"migration record at {record} — retirement must be distinguishable "
            f"from tampering (spec §11, protocols plan §S3)."
        )


def test_no_scenario_is_contested() -> None:
    # fork-multiple-terminals-partial-failure was adjudicated FROZEN (pure
    # fan-out, LEGAL per §7 rule 2; spec rev 3.2 corrected the rev 3.1
    # misnaming). CONTESTED remains as the fail-closed class for any FUTURE
    # unadjudicated entry — the set must stay empty.
    contested = sorted(n for n, c in SCENARIO_CLASSIFICATION.items() if c is OracleClass.CONTESTED)
    assert contested == []


def test_ws1_and_ws2_delta_classes_match_the_ratified_adjudication() -> None:
    assert SCENARIO_CLASSIFICATION["row-union-interleave"] is OracleClass.REGENERATED_WS1
    assert SCENARIO_CLASSIFICATION["parallel-coalesces"] is OracleClass.RULING_CASUALTY_WS2
    # Spec rev 3.2: pure fan-out, fully unbound, stays legal/frozen — the
    # unbound-fork audit shape must survive the WS1b flip byte-identical.
    assert (
        SCENARIO_CLASSIFICATION["fork-multiple-terminals-partial-failure"]
        is OracleClass.FROZEN
    )


def test_invariant_subset_excludes_token_keys_but_pins_sink_bytes_and_dispositions() -> None:
    surface = {
        "scenario_id": "s",
        "case_id": "c",
        "status": "completed",
        "rows": [2, 1, 1],
        "sink_outputs": [{"sink_name": "out", "rows": ['{"a":1}']}],
        "tokens": [{"key": "src:0#0"}],
        "terminal_dispositions": [
            {
                "key": "k1",
                "token_key": "src:0#0",
                "outcome": "success",
                "path": "coalesced",
                "sink_name": "out",
            }
        ],
        "expansions": [],
    }
    reshuffled = dict(
        surface,
        tokens=[{"key": "src:0#1"}],
        terminal_dispositions=[
            dict(surface["terminal_dispositions"][0], key="k9", token_key="src:0#1")
        ],
    )
    assert invariant_subset(surface) == invariant_subset(reshuffled)
    changed_sink = dict(surface, sink_outputs=[{"sink_name": "out", "rows": ['{"a":2}']}])
    assert invariant_subset(surface) != invariant_subset(changed_sink)
    changed_path = dict(
        surface,
        terminal_dispositions=[
            dict(surface["terminal_dispositions"][0], path="scope_group_failed")
        ],
    )
    assert invariant_subset(surface) != invariant_subset(changed_path)


def test_canonical_bytes_are_deterministic_and_newline_terminated() -> None:
    surface = {"b": 1, "a": [None, "x"]}
    first = canonical_bytes(surface)
    assert first == canonical_bytes(json.loads(first.decode("ascii")))
    assert first.endswith(b"\n")
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/unit/architecture/test_oracle_freeze_registry.py -v`
Expected: FAIL at import — `ModuleNotFoundError`/`ImportError`: no module
`tests.fixtures.dag_scenario_corpus.oracle_freeze`.

- [ ] **Step 3: Write the registry module**

Create `tests/fixtures/dag_scenario_corpus/oracle_freeze.py`:

```python
"""Frozen-oracle registry for the unified-lineage campaign (spec §11).

Snapshots live at oracle_freeze/v1/<scenario_id>/<case_id>.json, written ONCE
at the recorded pre-flip HEAD — after WS1a Task 8a's nested fixtures land,
before the WS1b flip commit (WS1b Task 7) begins (ELSPETH_ORACLE_FREEZE=write).
The stored bytes — not the harness that produced them — are the WS1 delta
oracle: the harness itself reads the retired tri-columns and is rewritten at
the flip, and a rebuilt harness verifying its own rebuild is the self-oracle
shape §11 forbids.

The surface deliberately excludes audit_records and manifest blobs, so the new
audit tables (token_lineage_frames, group_records, group_losses) never churn
frozen bytes (fixture-oracle scout, Risk 4).
"""

from __future__ import annotations

import enum
import json
from collections.abc import Mapping
from pathlib import Path

from tests.fixtures.dag_scenario_corpus.schema import ScenarioRunEvidence

FREEZE_ROOT = Path(__file__).parent / "oracle_freeze" / "v1"
RETIRED_ROOT = Path(__file__).parent / "oracle_freeze" / "retired"


class OracleClass(enum.Enum):
    """Per-workstream oracle disposition (spec §11 vocabulary)."""

    FROZEN = "frozen"
    REGENERATED_WS1 = "regenerated-ws1"
    RULING_CASUALTY_WS2 = "ruling-casualty-ws2"
    CONTESTED = "contested"


SCENARIO_CLASSIFICATION: dict[str, OracleClass] = {
    "linear": OracleClass.FROZEN,
    "multiple-independent-sources": OracleClass.FROZEN,
    "multi-source-queue-fan-in": OracleClass.FROZEN,
    "conditional-routing": OracleClass.FROZEN,
    # Pure fan-out (both branches direct to sinks, no closer) — LEGAL per §7
    # rule 2. Spec rev 3.2 corrected the rev 3.1 "mixed-fork r23 casualty"
    # misnaming; FROZEN permanently, and the unbound-fork audit shape must
    # survive the WS1b flip byte-identical. (parallel-coalesces below is the
    # actual r23 casualty.)
    "fork-multiple-terminals-partial-failure": OracleClass.FROZEN,
    "fork-coalesce-policies": OracleClass.FROZEN,
    # Sequential, not nested: two depth-1 regions in series. Frozen as-is; the
    # genuinely nested differential fixtures (fork-in-fork depth-2,
    # expand-in-fork) are authored by WS1a Task 8a pre-flip and join this
    # table as FROZEN entries (protocols plan §S1 ordering).
    "sequential-nested-fork-coalesce": OracleClass.FROZEN,
    # ONE fork closing at TWO sibling coalesces — ruling 23 casualty, leaves the
    # frozen set at WS2 via §S3.
    "parallel-coalesces": OracleClass.RULING_CASUALTY_WS2,
    "aggregation-immutable-batch": OracleClass.FROZEN,
    "row-expansion-parent-child-recovery": OracleClass.FROZEN,
    # Ruling 27 pops branch frames on release: released tokens' branch_name goes
    # to None, and branch_name is a harness SORT key, so token-key ordinals can
    # reshuffle — a whole-projection WS1 delta adjudicated by hand (§S1), with
    # the invariant subset below enforced automatically forever.
    "row-union-interleave": OracleClass.REGENERATED_WS1,
    "retry-quarantine-discard-routed-errors": OracleClass.FROZEN,
    "sink-write-pending-redrive": OracleClass.FROZEN,
    "checkpoint-deterministic-resume": OracleClass.FROZEN,
    # pytest-evidence only: no v1 fixture directory, no harness cases to snapshot.
    "multi-worker-lease-reclaim-late-completion": OracleClass.FROZEN,
}


def frozen_surface(evidence: ScenarioRunEvidence) -> dict[str, object]:
    """The §11 frozen-invariant slice of one harness case's run evidence.

    Exactly the four stable projection classes plus the run summary — never
    audit_records, never manifest material, never raw group ids.
    """
    runtime = evidence.runtime
    surface: dict[str, object] = {
        "scenario_id": evidence.scenario_id,
        "case_id": evidence.case_id,
        "status": runtime.status,
        "rows": [runtime.rows_processed, runtime.rows_succeeded, runtime.rows_failed],
        "sink_outputs": [s.model_dump(mode="json") for s in runtime.sink_outputs],
    }
    projection = runtime.durable_projection
    if projection is not None:
        surface["tokens"] = [t.model_dump(mode="json") for t in projection.tokens]
        surface["terminal_dispositions"] = [
            d.model_dump(mode="json") for d in projection.terminal_dispositions
        ]
        surface["expansions"] = [e.model_dump(mode="json") for e in projection.expansions]
    return surface


def canonical_bytes(surface: Mapping[str, object]) -> bytes:
    return (
        json.dumps(surface, sort_keys=True, indent=1, ensure_ascii=True).encode("ascii")
        + b"\n"
    )


def snapshot_path(scenario_id: str, case_id: str) -> Path:
    return FREEZE_ROOT / scenario_id / f"{case_id}.json"


def invariant_subset(surface: Mapping[str, object]) -> dict[str, object]:
    """The always-frozen slice for REGENERATED_WS1 fixtures.

    Ruling 27's pop may reshuffle token-key ordinals (branch_name is a harness
    sort key), so token keys and disposition keys are excluded; sink bytes,
    run summary, and the disposition MULTISET of (outcome, path, sink_name,
    error_hash) must never move.
    """
    dispositions = surface.get("terminal_dispositions") or []
    multiset = sorted(
        json.dumps(
            [
                entry.get("outcome"),
                entry.get("path"),
                entry.get("sink_name"),
                entry.get("error_hash"),
            ]
        )
        for entry in dispositions
    )
    return {
        "status": surface.get("status"),
        "rows": surface.get("rows"),
        "sink_outputs": surface.get("sink_outputs"),
        "disposition_multiset": multiset,
    }
```

- [ ] **Step 4: Run the registry tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/architecture/test_oracle_freeze_registry.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/dag_scenario_corpus/oracle_freeze.py tests/unit/architecture/test_oracle_freeze_registry.py
git commit -m "test(corpus): oracle-freeze registry and per-workstream classification for the unified-lineage campaign"
```

---

### Task 2: Snapshot writer / compare suite

**Files:**
- Create: `tests/integration/core/dag/test_oracle_freeze.py`

**Interfaces:**
- Consumes: Task 1's module (every exported name above);
  `tests/fixtures/dag_scenario_corpus/harness.py::run_scenario_case(scenario: ScenarioSpec, case: HarnessCaseSpec, tmp_path: Path) -> ScenarioRunEvidence`
  (`harness.py:5230`);
  `tests/fixtures/dag_scenario_corpus/loader.py::load_manifest()` / `iter_harness_cases(manifest) -> tuple[tuple[ScenarioSpec, HarnessCaseSpec], ...]`;
  `tests/fixtures/dag_scenario_corpus/plugins.py::install_corpus_plugin_manager(monkeypatch)`;
  `ScenarioSpec.id`, `HarnessCaseSpec.id`, `HarnessCaseSpec.expected`
  (`BuildExpectation` cases have no runtime and are excluded);
  `tests/fixtures/dag_scenario_corpus/schema.py::BuildExpectation`.
- Produces: the campaign's continuously-enforced oracle gate (§S2 step 7) and the
  write-mode contract `ELSPETH_ORACLE_FREEZE=write` used by Task 3 and §S1.

- [ ] **Step 1: Write the suite (it must FAIL before the freeze runs — no snapshots exist yet)**

Create `tests/integration/core/dag/test_oracle_freeze.py`:

```python
"""WS1 frozen-oracle gate (unified-lineage protocols plan §S1/§S2; spec §11).

Modes:
  default                       — recompute each case's frozen surface through the
                                  live harness and compare against the STORED
                                  snapshot bytes (the pre-flip record).
  ELSPETH_ORACLE_FREEZE=write   — (re)write snapshots. Legitimate ONLY at the
                                  recorded pre-flip freeze commit (protocols plan
                                  Task 3) or an §S3-adjudicated retirement; any
                                  other rewrite is oracle tampering.

CAMPAIGN INSTRUMENT: this suite re-runs every corpus harness case (~minutes).
It exists for the duration of the unified-lineage campaign and IS DELETED with
the campaign's closing slice (prerelease no-tech-debt posture; ratified
2026-08-22 — do not promote it).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.fixtures.dag_scenario_corpus import oracle_freeze
from tests.fixtures.dag_scenario_corpus.harness import run_scenario_case
from tests.fixtures.dag_scenario_corpus.loader import iter_harness_cases, load_manifest
from tests.fixtures.dag_scenario_corpus.plugins import install_corpus_plugin_manager
from tests.fixtures.dag_scenario_corpus.schema import BuildExpectation, HarnessCaseSpec, ScenarioSpec

_MANIFEST = load_manifest()
_CASES = [
    (scenario, case)
    for scenario, case in iter_harness_cases(_MANIFEST)
    if not isinstance(case.expected, BuildExpectation)
]
_WRITE = os.environ.get("ELSPETH_ORACLE_FREEZE") == "write"


@pytest.mark.parametrize(
    ("scenario", "case"),
    _CASES,
    ids=[f"{scenario.id}--{case.id}" for scenario, case in _CASES],
)
def test_frozen_oracle_surface(
    scenario: ScenarioSpec,
    case: HarnessCaseSpec,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    classification = oracle_freeze.SCENARIO_CLASSIFICATION[scenario.id]
    if classification is oracle_freeze.OracleClass.CONTESTED:
        pytest.fail(
            f"Scenario {scenario.id!r} is CONTESTED in the oracle classification: "
            "maintainer adjudication is required before the freeze is "
            "authoritative (protocols plan §S1). No scenario is contested today; "
            "a new CONTESTED entry fails closed here by design."
        )

    install_corpus_plugin_manager(monkeypatch)
    evidence = run_scenario_case(scenario, case, tmp_path)
    surface = oracle_freeze.frozen_surface(evidence)
    path = oracle_freeze.snapshot_path(scenario.id, case.id)

    if _WRITE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(oracle_freeze.canonical_bytes(surface))
        return

    if not path.exists():
        pytest.fail(
            f"No frozen snapshot at {path} — run the freeze writer at the recorded "
            "pre-flip commit (protocols plan Task 3) before the WS1b flip "
            "(WS1b Task 7) starts."
        )

    stored = path.read_bytes()
    if classification is oracle_freeze.OracleClass.REGENERATED_WS1:
        stored_surface = json.loads(stored.decode("ascii"))
        assert oracle_freeze.invariant_subset(surface) == oracle_freeze.invariant_subset(
            stored_surface
        ), (
            f"REGENERATED_WS1 fixture {scenario.id}/{case.id} moved OUTSIDE the "
            "invariant subset (sink bytes / disposition multiset / run summary) — "
            "that is a behaviour delta, not the adjudicated ruling-27 representation delta."
        )
    else:
        assert oracle_freeze.canonical_bytes(surface) == stored, (
            f"FROZEN fixture {scenario.id}/{case.id} diverged from the pre-flip "
            "snapshot. Per spec §11 this fails the WS1 checkpoint: STOP, do not "
            "regenerate the snapshot, and surface the delta to the maintainer."
        )


def test_no_snapshot_exists_without_a_live_manifest_case() -> None:
    """A snapshot whose case left the manifest must go through §S3 retirement."""
    if not oracle_freeze.FREEZE_ROOT.exists():
        pytest.skip("freeze not yet executed (protocols plan Task 3)")
    live = {
        (scenario.id, case.id)
        for scenario, case in _CASES
    }
    stored = {
        (p.parent.name, p.stem)
        for p in oracle_freeze.FREEZE_ROOT.glob("*/*.json")
    }
    orphans = sorted(stored - live)
    assert orphans == [], (
        f"Snapshots with no live manifest case: {orphans} — retire them via "
        "protocols plan §S3 (git mv to oracle_freeze/retired/ + MIGRATION.md), "
        "never delete in place."
    )
```

- [ ] **Step 2: Run compare mode to verify it fails closed (no snapshots exist yet)**

Run: `.venv/bin/pytest tests/integration/core/dag/test_oracle_freeze.py -q -x`
Expected: FAIL — "No frozen snapshot at …" for the first collected case. (The designed
pre-freeze state; Task 3 clears it. No scenario is CONTESTED — the classification is
fully adjudicated, see Task 1.)

- [ ] **Step 3: Commit (the suite lands red-by-design behind the freeze; commit it with Task 3's freeze in one slice if the repo's CI-green discipline requires — otherwise commit now and execute Task 3 immediately after)**

```bash
git add tests/integration/core/dag/test_oracle_freeze.py
git commit -m "test(corpus): frozen-oracle snapshot writer/compare gate for the WS1 checkpoint"
```

---

### Task 3: Execute the freeze at the recorded pre-flip HEAD

Runs after WS1a Task 8a lands and before the WS1b flip commit (WS1b Task 7) begins —
§S1 ordering.

**Preconditions (all must hold — §S1 ordering):**
1. WS0 plan complete and merged.
2. **WS1a Task 8a** complete: the nested differential fixtures (fork-in-fork depth-2
   and expand-in-fork scenario dirs under `tests/fixtures/dag_scenario_corpus/v1/`)
   are landed, wired into `EXPECTED_SCENARIOS`, and classified FROZEN in
   `SCENARIO_CLASSIFICATION` — consult WS1a Task 8a's Produces block for the new
   scenario ids.
3. Working tree clean; sibling agents notified (long corpus runs on the shared checkout
   need the tree frozen — record HEAD before/after).

**Files:**
- Create: `tests/fixtures/dag_scenario_corpus/oracle_freeze/v1/**/*.json` (written by
  the suite, committed as reviewed bytes)

**Interfaces:**
- Consumes: Task 2's write mode.
- Produces: the pre-flip oracle bytes every WS1b slice compares against; the freeze
  commit hash, recorded below and in the WS1b plan's preamble.

- [ ] **Step 1: Record HEAD and run the writer**

```bash
git rev-parse HEAD | tee /tmp/claude-1000/freeze-head-before
ELSPETH_ORACLE_FREEZE=write .venv/bin/pytest tests/integration/core/dag/test_oracle_freeze.py -q
git rev-parse HEAD | tee /tmp/claude-1000/freeze-head-after
diff /tmp/claude-1000/freeze-head-before /tmp/claude-1000/freeze-head-after
```
Expected: writer green; HEADs identical (else discard the snapshots and re-run).

- [ ] **Step 2: Immediately re-run in compare mode against the fresh bytes**

Run: `.venv/bin/pytest tests/integration/core/dag/test_oracle_freeze.py -q`
Expected: all pass — proves write→compare round-trips byte-identically on the same
tree, so any later divergence is a real delta, not serialization noise.

- [ ] **Step 3: Review and commit the snapshot bytes**

```bash
git status --porcelain tests/fixtures/dag_scenario_corpus/oracle_freeze/
# review: every file is a new v1/<scenario>/<case>.json; no other paths touched
git add tests/fixtures/dag_scenario_corpus/oracle_freeze/
git commit -m "test(corpus): pre-flip frozen-oracle snapshot at $(cat /tmp/claude-1000/freeze-head-before)"
```

- [ ] **Step 4: Record the freeze commit** — paste `git rev-parse HEAD` into the WS1b
  plan's preamble ("oracle frozen at <sha>") so every WS1b slice can verify it is
  diffing against the right bytes.

---

### Task 4: Ruling-casualty migration sweep worklist

These are the concrete migrations the rulings force, each executed at its trigger
point by the named owner plan; this plan tracks them so none is silently dropped.
Verified inventory (fixture-oracle scout): NO r25 casualties and NO r28 casualties
exist in the corpus or `examples/` — the whole worklist is below.

- [ ] **RC-1 (trigger: the WS1b flip commit, WS1b Tasks 7–12; owner: WS1b plan) —
  `row-union-interleave` regeneration.** Ruling 27 pops branch frames on release.
  Adjudicate as a WHOLE-PROJECTION delta: `branch_name` leaves every released token,
  and because `branch_name` is a harness sort key, `#ordinal` token keys may reshuffle
  and rename every keyed record. Required-identical: sink bytes, disposition multiset,
  run summary (enforced automatically by `invariant_subset`). Rotate the manifest's
  `projection_sha256`/`exact` blobs for this scenario with a dated A/B note in the
  `test_dag_scenario_corpus_contract.py` ledger + `EXPECTED_CASE_REGISTRY_SHA256`
  rotation. The pre-flip snapshot under `oracle_freeze/v1/row-union-interleave/` is
  NEVER rewritten (it is the permanent pre-rewrite record).

- [x] **RC-2 — RESOLVED (spec rev 3.2, 2026-08-22): `fork-multiple-terminals-partial-failure`
  is NOT a casualty.** Its verified topology is pure fan-out (both branches direct to
  sinks, no closer), which §7 rule 2 makes LEGAL; the rev 3.1 "mixed-fork r23
  casualty" naming was the spec example being wrong, and rev 3.2 corrected it. The
  fixture is FROZEN permanently, and the unbound-fork audit shape must survive the
  WS1b flip byte-identical. Recorded in the classification table and the registry pins
  (Task 1); `parallel-coalesces` is the actual r23 casualty (RC-3).

- [ ] **RC-3 (trigger: folded into WS2 Task 6's commit — whole-roster fork closure,
  rule 2 / ruling 23; owner: WS2 plan) — `parallel-coalesces` rewrite.** Today: ONE
  fork `fork_to [left_a, left_b, right_a, right_b]` closing at TWO sibling coalesces —
  r23 rejects it. Replacement preserving the "two coalesces run in parallel" pedagogy:
  an outer pure fan-out fork `[left, right]` (legal per rule 2 — RC-2's resolution
  confirms pure fan-out stays expressible), each branch containing an inner
  whole-roster fork `[<side>_a, <side>_b]` closing at its own coalesce, each merged
  token continuing to its own sink. Retire the old scenario via §S3 (manifest surgery
  + rotation ledger + `git mv` to `oracle_freeze/retired/parallel-coalesces/`
  + `MIGRATION.md` naming ruling 23) — all in WS2 Task 6's commit.

- [ ] **RC-4 (trigger: folded into WS2 Task 7's commit — bidirectional SESE walk,
  rule 4; owner: WS2 plan + maintainer) —
  `examples/row_union_ab_experiment/settings_screened.yaml`.** The
  `quality_screen` gate inside the control branch routes `'false'` to the
  `screened_out` sink — a path from opener to sink before the closer, rejected flat by
  rule 4. NO mechanical migration exists: the example's pedagogical point is the
  prohibited shape. **RULED (maintainer, 2026-08-22, on joint arch+systems advice):
  TWO variants.** (i) `settings_screened.yaml` is rewritten as screen-BEFORE-fork on
  `baseline_quality >= 60` — a SOURCE-known predicate, so pre-group screening is the
  correct engineering, the `screened_out` sink stays (it sits outside the region), the
  treatment arm is never forked/billed for screened tickets, and the run goes
  SUCCESS/exit 0. (ii) NEW `settings_screened_at_settlement.yaml` demonstrates
  fail-closed group settlement with a predicate genuinely unknowable pre-fork — the
  in-branch screen keys on the computed `score` (post-`tag_control`) and routes the
  screened row to discard so the settle-member seam stages the member loss and the
  `require_all` union fails that ticket's pair closed. Its README states the costs
  plainly: the sibling arm is billed then terminated `scope_group_failed`, the run is
  PARTIAL/exit 1 by design, and screened rows are recovered from the audit trail
  (`group_losses` + landscape queries), not a sink. Both variants' README output-count
  prose (routed counts, disposition names) is rewritten as part of the migration; the
  comparison statistics section survives verbatim in both (same 5 survivors, same
  numbers). Implement both in WS2 Task 7's commit and update `examples/AGENTS.md` run
  notes.

- [ ] **RC-5 (trigger: WS2, before the Task 6/Task 7 rejections land; owner: WS2
  plan) — casualty-grep
  drift check.** The zero-r25/zero-r28 inventory was measured at HEAD `add597342`;
  re-verify at the WS2 slice's HEAD before landing the rejections:
  `git grep -l "fork_to" examples/ tests/fixtures/dag_scenario_corpus/v1/` and inspect
  each hit for (i) aggregation nodes inside a fork/union branch (r25), (ii) multi-row
  transforms inside a bound branch (r28), (iii) branch subsets closing at a coalesce
  (r23). Any NEW hit since the scout is a new casualty: add it to this worklist with
  its own adjudication before the rejection lands.

- [ ] **RC-6 (trigger: WS1a Task 8a fixture authoring; owner: WS1a plan; tracked
  here) — corpus schema extensions for the new fixtures.** Two closed contracts block
  spec-required fixtures and must be extended as reviewed changes landing WITH the new
  fixtures, existing frozen fixtures untouched: (i)
  `StableExpansionProjection.expected_child_count: PositiveCount` (ge=1) cannot
  represent the required `member_count=0` empty-expansion record — add an explicit
  zero-legal representation for the new empty-expansion fixture; (ii) the
  `StableTerminalDisposition.path` 14-value Literal lacks `scope_group_failed`,
  `empty_expansion`, `all_members_lost` — extend the Literal in the same reviewed
  change. Both edits land BEFORE Task 3's freeze so the new fixtures freeze with
  everything else.

- [ ] **RC-7 (decision, stated here for every implementer; consumer: WS2 plan) — SESE
  forward-walk scope over error edges.** Every fork-coalesce loss fixture terminates
  tokens in-region via `on_error: discard`/routed errors — that is the settlement
  system's INPUT, and §7 rule 9 treats a bound region's closer as a legal in-region
  `on_error` target. Decision (derived from rule 9, recorded here so rule 4 is not
  read literally): **the §7 rule 4 forward walk covers success-path and gate-route
  edges ONLY; `on_error` edges are excluded from "every path from the opener reaches
  the closer before any sink/terminal".** Losses reach the roster through the
  settlement channel, not through SESE path coverage. The WS2 plan pins this with a
  build-acceptance test: the 8 existing lost-branch corpus fixtures must remain
  buildable under the new rules.

---

### Task 5: Verify the standing-procedures cross-references (verification only)

**Files:** none. Every sibling campaign plan already carries the citation line in its
Global Constraints ("Standing procedures: docs/superpowers/plans/2026-08-21-unified-lineage-protocols.md
§S1–§S5 govern fixture freezing, slice gates, casualty retirement, judge-bundle
sequencing, and the WS1 STOP rule") — added in the 2026-08-22 cross-plan review round.
This task authors nothing; it verifies the wiring held.

- [ ] **Step 1: Verify every campaign plan references this plan**

Run: `grep -L "unified-lineage-protocols.md" docs/superpowers/plans/2026-08-21-unified-lineage-*.md`
Expected: EMPTY output (this file cites itself as the source of record, so even it
matches). Any listed file is a sibling plan whose citation line regressed — that is a
defect to surface to the maintainer, not something this task quietly re-authors.

- [ ] **Step 2: Verify the judge-staging precondition once, at campaign start**

Call `mcp__elspeth-judge__stage_status`; record the result in the campaign log. If a
bundle is staged, STOP and surface to the maintainer before any WS1 slice (§S4).

---

## Self-review notes

- Spec coverage: §11 frozen-oracle protocol → §S1 + Tasks 1–3; per-workstream oracle
  versioning → §S3 + registry enforcement in Task 1's orphan test and Task 2's
  no-orphan-snapshot test; ruling casualties (23/25/27/28/§7-rule-4) → Task 4 RC-1..7;
  whole-tree gate obligations → §S2; judge sequencing → §S4; checkpoint/STOP → §S5
  (verbatim quote).
- The freeze surface excludes `audit_records`, `node_states`, `routes`,
  `scheduler_work`, and manifest blobs BY DESIGN: §11 names the four stable projection
  classes as the delta oracle; the excluded surfaces churn legitimately at WS1 (new
  audit rows) or WS3 (scheduler-event counts, fixture-oracle scout Risk 10) and are
  adjudicated through the manifest-rotation ledger instead.
- Type consistency: `frozen_surface(evidence: ScenarioRunEvidence)` is consumed with
  that exact signature in Task 2; `invariant_subset` takes the surface dict (or its
  JSON round-trip) in both the unit test and the compare suite.
- Cost stated honestly: the compare suite re-runs every runtime harness case per full
  suite. It is a campaign instrument, DELETED at the campaign's closing slice
  (ratified 2026-08-22, prerelease no-tech-debt posture; its own module docstring says
  so); if the maintainer finds the duplicate cost unacceptable mid-campaign, gating it
  to slice boundaries is a one-line marker change — but it ships default-on because a
  gate that does not run protects nothing.

## Open Questions

None — the campaign's decisions are all closed.

Resolved since the 2026-08-21 draft (2026-08-22 synthesis round — do not reopen):
**`settings_screened.yaml` replacement RULED 2026-08-22: two variants (see RC-4)** —
screen-before-fork for the source-known `baseline_quality` predicate, plus a new
`settings_screened_at_settlement.yaml` demonstrating screen-as-loss on the computed
score;
`fork-multiple-terminals-partial-failure` is pure fan-out, LEGAL, FROZEN (spec rev 3.2;
RC-2); the three new audit tables enter the portable export at the WS1b flip (§S1);
sibling task citations are now concrete (WS1a Task 8a, WS1b Task 7, WS2 Tasks 6/7);
the freeze compare suite is DELETED at campaign close.
