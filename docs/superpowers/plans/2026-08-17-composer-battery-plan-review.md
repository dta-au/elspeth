# Plan review record — composer battery implementation plan (435a28966 → revised)

Four Opus reviewers (axiom-planning architecture / quality / reality / systems) reviewed `docs/superpowers/plans/2026-08-17-composer-battery.md` at `435a28966` on 2026-08-17. Every finding was adjudicated by the plan author; the accepted/declined list and the deviations-from-spec list live in the plan's **Self-review and review adjudication** section. The plan's code blocks were then materialised into a scratch tree and executed: 93 unit tests pass across Tasks 1–8 (with two seed cases), and `ruff` reports only auto-fixable import ordering plus the CLI `print`s covered by the per-file-ignore added in Task 7.

---

## 1. Architecture lens

# Architecture Review — Composer Path-Quality Battery plan

Plan: `docs/superpowers/plans/2026-08-17-composer-battery.md` @ 435a28966 ·
Spec: `docs/superpowers/specs/2026-08-13-composer-battery-design.md` (rev 4)
Lens: module boundaries, coupling, duplication, technical debt. Read-only.

## Blast radius

**Existing code touched: one file** — `.gitignore`. Everything else is new: 6
`evals/lib/battery_*.py`, 3 scripts, 19 `scenario.json`, `corpus.md`, 8 test
modules. No core logic, no schema, no migration, no API contract, no one-way
door: `runs/` is ignored, the driver is compose+validate only and never calls
`/execute`. Weighted risk **Low** on the existing tree; the real risk is ~2,500
lines of *new* surface landing as one branch, mitigated by per-task commits.
**Proceed**, with Task 3 split (finding 4).

---

## Findings

### 1. [High] `score_run` fuses a scenario-free path pass with a scenario-bound judgement — both other consumers pay for it

*Task 5 `score_run`; Task 7 `Battery._verdict`; Task 8 `scenario_for_fixture`.*

The exclusion block (L2098–2119) reads only `capture.meta`, `surface_of`, LLM
statuses and `is_valid` — **zero scenario input** — but is reachable only via
`score_from_disk(run_dir, scenario)`. Consequences:

- The **live driver** loads `scenario.json` per run (L3290–3294) and drags the
  entire scoring stack — topology comparator, pydantic, the web catalog — into
  a multi-hour live firing just to read `.excluded`. `_excluded_stub` even
  takes `scenario.floor.tool_bearing_calls` for a field the abort rule ignores.
- The **probe** fabricates a `Scenario` (L3748–3750) with `floor=2` and an
  invented `derivation` so it can reuse `score_run`; `score_arm` then folds the
  resulting `unattributed_excess` into `ArmResult.deviations` and `clean`. A
  probe arm can be reported non-clean against a floor nobody pre-registered —
  inverting the plan's own rule that floors are pre-registered evidence.

**Fix (one cut, both symptoms):** `score_path(capture) -> PathScore` (buckets,
deviations, surface, exclusions; no scenario) and `judge(scenario, path) ->
Score` (floor, excess, topology, green/red). Driver imports the first only and
drops `battery_scenario`; probe imports the first only and deletes
`scenario_for_fixture`; corpus scoring composes both. Do this first — findings
2 and 8 land in the same module.

### 2. [High] The abort rule treats a product measurement as an instrument fault

*Task 7 `fire`; Task 5 `EXCLUSION_KINDS`.*

`excluded` includes `surface`, which fires when the composer routed to the
**planner** — arguably the most interesting finding the battery can produce.
Three in a row kill the round with `abort_reason="3 consecutive
instrument_error"` and discard the signal. The CI gate
(`test_prompt_stays_on_the_compose_loop`) pins the *source tree*, not the
substrate, so deployed-grammar drift is exactly the scenario that trips this.
The plan's own `test_fire_aborts_after_three_consecutive_instrument_errors`
triggers the abort using `surface` exclusions — the conflation is encoded.

**Fix:** partition beside `score_path`: `INSTRUMENT_KINDS = (capture,
truncated, read_integrity, auth, http, transport, terminal_missing)` vs
`MEASUREMENT_KINDS = (surface, no_calls)`; abort and case-flag on the former
only, report the latter in its own block. That test must be rewritten. While
there, lift `should_abort(verdicts) -> str | None` out of `Battery` as a free
function — it deletes the test's `run_prompt` monkeypatch (L2921) and the
`Battery.last_label` attribute that exists only for a test (L2996).

### 3. [High] Tool-row parsing is forked for the *third* time, against explicit spec direction, and unpinned

*Task 4 `battery_capture`.*

Spec L59–63: *"The battery **imports, not forks**: `evals/lib/composer_rgr_score.py`
(tool-row parsing, red/green criteria — the helpers are private today and get
public names when imported)"*, with a spec open-question item *"Give the RGR
tool-row helpers public names before importing them."* The plan silently
re-implements instead, and this deviation is **absent** from its own
deliberate-deviation list (L4079, items a–f).

There are now three derivations of the same projection:
`web/sessions/routes/_helpers.py:422 _tool_call_outcomes_by_call_id` (server),
`evals/lib/composer_rgr_score.py:524 _tool_result_by_call_id` (rgr, shared by
three harnesses), and the plan's `tool_outcomes`. The fork is *technically*
justified — the server helper takes `Sequence[ChatMessageRecord]` +
`state_versions_by_id`, so it cannot run on wire JSON — but its only defence is
a manual `git grep` instruction (L1346), and
`_FAILED_STATUSES = {"arg_error","plugin_crash"}` hardcodes values of an
importable `ComposerToolStatus` (`contracts/composer_audit.py:41`). Per project
doctrine that pins existence, not truth.

**Fix:** (a) build status sets from `ComposerToolStatus.*.value`; (b) add a
characterization test constructing `ChatMessageRecord`s from the same wire rows
and asserting agreement with the server projection across applied / rejected /
failed / cancelled / lying-stamp shapes; (c) lift the shared wire-row parsing
into `evals/lib/` and have rgr use it too — note the battery's use of the
registry's `is_mutation_tool` is *better* than rgr's hand-maintained
`_MUTATING_TOOL_NAMES` (`:474`), so unify upward, not downward; (d) whichever
path is taken, record it in the deviation list.

### 4. [High] Test-collection coupling makes the corpus a 19-way all-or-nothing gate

*Task 3 Step 7; test modules of Tasks 5–8.*

`test_corpus.py` hardcodes a 19-name `EXPECTED_CASES` and asserts set equality
with the scenarios dir; the Task 5/6/7 test modules call `load_scenario` at
**module import time** (L1499, L2201–2202, L2772–2773). Until all 19 scenarios
exist and validate, `pytest tests/` fails at *collection*, and one malformed
`scenario.json` takes out four unrelated modules. The plan half-knows it —
Task 6 Step 2 carries the workaround *"author `canary` first"*. Task 3 Steps
6–7 (16 hand-written prompts + 18 hand-written scenarios) is the longest, least
mechanical stretch in the plan, so the tree stays red throughout it.

**Fix:** derive `EXPECTED_CASES` from `load_corpus()` (corpus becomes the single
roster); move module-level `load_scenario` into session-scoped fixtures; split
Task 3 into 3a (parser + `.gitignore` + 3 seed cases) and 3b–3d (strata
batches). Sibling precedent for a partial corpus: `pytestmark = skipif(...)` in
`tests/unit/evals/test_convergence_scenarios.py:27`.

### 5. [Medium-High] `planner_probe.py` is a library living in the driver directory, which forces a new `sys.path` pattern into `tests/`

*Task 8; Task 7 Step 1 conftest.*

The established evals convention is: pure logic in `evals/lib/<name>.py`,
unit-tested at `tests/unit/evals/lib/test_<name>.py` 1:1; hyphenated
`evals/<harness>/` holds drivers/corpus with a `sys.path` shim *in the script*
(`evals/composer-rgr/score.py:21`, two composer-harness shims). Today there are
**zero `sys.path` hits under `tests/unit/evals/`**.

`planner_probe.py` is ~90% offline scoring (`score_arm`, `score_probe_dir`,
`score_tripwire_dir`, `required_information`, `triage_code`, `staged_topology`)
with ~20 lines of live driving. Placing it under `evals/composer-battery/`
forces `tests/unit/evals/composer_battery/conftest.py` to insert a hyphenated
dir on `sys.path` (L2742–2749) so two test modules can `import planner_probe` —
a new pattern in the test tree, and the reason the plan has to check for
conflicts with existing conftests. It also puts tests for six `evals/lib/`
modules under `composer_battery/` instead of `tests/unit/evals/lib/`, breaking
the mirror.

**Fix:** move the offline half to `evals/lib/battery_planner.py`; leave
`run_probe`/`run_tripwire` in `evals/composer-battery/planner_probe.py` with the
standard in-script shim. The conftest hack then covers only `drive_battery`
(genuinely a driver), or disappears if the driver's testable policy —
`should_abort`, pagination, review loop — also moves to `evals/lib/`. Put
`test_battery_{topology,scenario,capture,score,report,planner}.py` under
`tests/unit/evals/lib/`.

### 6. [Medium-High] No ignore-contract test for the project's second tracked, credential-handling harness

*Task 3 Step 1.*

`evals/composer-parity/` is the only tracked harness today and it carries
`tests/unit/evals/composer_parity/test_ignore_policy.py`, which asserts
credential-shaped paths under the corpus stay `git check-ignore`d. The plan
copies parity's `.gitignore` re-include block but verifies it with a **manual
`git check-ignore` instruction inside a step** — no test. The battery is
strictly higher-risk than parity: it reads `~/.elspeth-battery/credentials.json`,
holds a live bearer token, and captures full session threads into `runs/`.
Broad `!/evals/composer-battery/**` negations are exactly the rule that a later
edit re-orders into a credential leak, and the pre-commit secret scanner is a
line-content check, not a path-policy check.

**Fix:** mirror `test_ignore_policy.py` for `evals/composer-battery/` in Task 3,
asserting `runs/`, `jwt.txt`, `login.json`, `*.access_token`, `credentials.json`
resolve to ignored — and that `corpus.md` / `scenarios/**` do not.

### 7. [Medium-High] Floors can be revised after seeing the data, and `--compare` will attribute the shift to the model

*Task 2 `Floor.post_calibration`; Task 6 `_compare`; Task 9 Step 5.*

`_compare` refuses on `identity.binding` mismatch and on `corpus_version`
mismatch. `identity.binding` carries substrate, models, budgets, timeouts —
**not floors, not a corpus hash**. Task 9 Step 5 bumps `corpus_version` at
freeze, but nothing enforces that a later `floor.post_calibration` edit bumps
it. Two rounds with revised floors and identical models therefore compare
cleanly, and the pooled `optimal_pp` delta reads as a model or kit change. The
same hole exists for the taxonomy: `report.py` re-scores captures offline, so a
`SEVERITY` edit silently rewrites an old round's `score.json`.

**Fix:** record `floors_sha256` (over sorted `(case, floor)` pairs) and
`scorer_taxonomy_sha` (over `SEVERITY` + `EXCLUSION_KINDS`) in
`report.json.identity.binding`, so the refusal mechanism that already exists
catches both; add a test asserting `post_calibration is not None ⇒
corpus_version >= 1`.

### 8. [Medium] The `meta.json` contract exists three times and is read defensively

*Task 5 prose (L1380); Task 7 `_write_meta`/`instrument` literals (L3165, L3287);
`threadgen.meta()` (L1444).*

Producer writes a bare dict, consumer reads `instrument.get("truncated")` /
`meta.get("http")`, test builder hand-writes a third copy. Rename
`http_unrecovered` → `http_error` in the driver and every affected run scores as
*included and clean* — no test fails, because the fixture builder carries the
new key too. This is the one boundary the battery genuinely owns, so ADR-032's
parse-what-you-don't-own posture applies.

**Fix:** `@dataclass Instrument` + `@dataclass RunMeta` with `to_dict`/`from_dict`
in the scenario-free module from finding 1; driver constructs, scorer parses
(missing/unknown key ⇒ `capture` exclusion with evidence), `threadgen` builds
from the same type.

### 9. [Medium] The live driver has no failure containment — one unconverted exception ends a multi-hour round

*Task 7 `RequestsClient.request`, `step`, `_settle`, `fire`.*

`RequestsClient` converts only `requests.Timeout`. A `ConnectionError`,
`SSLError` or `ChunkedEncodingError` propagates raw through `step()` (which
catches only `HttpTimeout`) → `run_prompt` → `fire`, which has no `try/except`.
The in-flight run's `meta.json` is never written and `firing.json` stops at the
previous run. Worse, `_settle` (L3255–3265) calls `self.client.request`
directly, bypassing `step()` — and `_settle` runs *only* on the
non-200/timeout path, so the place most likely to see a second failure is the
one place with no guard.

**Fix:** convert `requests.RequestException` → `HttpTransportError` in the
client; route `_settle` through `step`; wrap `_run_or_resume` in `fire` with
`except Exception` → record `excluded="transport"`, write `meta.json`, continue.

### 10. [Medium] Free-string vocabularies where enums exist — including one the interface list claims to consume

*Task 8; Task 2/5 criteria keys.*

- Task 8's interface block states it consumes `ComposerPlannerInformationClass`
  (`contracts/composer_planner_audit.py:78`) but the implementation never
  imports it; `LOOP_TOOL_TO_INFO` and `required_information` use literals
  (`"plugin.schema"`, `"model.catalog"`, `"catalog.selection"`) duplicating that
  enum's values, and `triage_code` hardcodes planner-code names with an
  `endswith("_EXHAUSTED")` catch-all. A server rename makes `floor_missing`
  permanently empty or permanently full with no test failing.
- `green_criteria`/`red_criteria` inherit rgr's **open** string vocabulary,
  which has *already* drifted three ways (`score()`'s `.get()` calls,
  `scenario_from_example.build_criteria_from_target`, and `_KNOWN_*_KEYS` in
  `test_convergence_scenarios.py:32-58` — which is missing
  `must_discover_schema_before_first_mutation`, a key the battery uses). The
  plan then reads it with **mixed defaults**:
  `gc.get("topology_matches_expected", True)` (typo ⇒ silently checked) vs
  `gc.get("must_discover_schema_before_first_mutation")` (typo ⇒ silently
  *un*checked). A misspelled criterion in one `scenario.json` disables a green
  gate for that case forever, invisibly.

**Fix:** map through the enum values and add a test asserting every
`LOOP_TOOL_TO_INFO` value and triage key is a live enum member; make the
criteria vocabulary **closed** — validate keys in `load_scenario` and raise on
unknown, with uniform defaults.

### Low, batch: convention divergences worth one decision each

Exit codes (siblings share 0/64 usage/67 missing input/69 tool/70 auth/71
network across bash and python CLIs; the plan uses driver 0/1, report 0/2);
credential env names (`ELSPETH_EVAL_BASE_URL/USER/PASS` established vs the
plan's new `BATTERY_USERNAME`/`BATTERY_PASSWORD` + `~/.elspeth-battery/`);
`--cleanup --cases none` in the README relies on a `"none"` sentinel that is not
a validated case name — prefer `--cleanup-only`. Pick deliberately and note it;
each is cheap now and annoying later.

---

## Strengths worth keeping

- **Blast radius of one existing file.** Nothing under `src/` moves; the
  instrument is deletable. Right shape for an eval harness.
- **Capture/score separation is real and correctly placed.** The driver captures
  and never scores for measurement; the report is offline over captured JSON, so
  a taxonomy revision re-scores history without re-firing. This is the design's
  most valuable property and it survives every finding above — finding 7 only
  asks that the re-score be attributable.
- **The driver is the anti-`scripts/acceptance_battery.py`.** That sibling has no
  injection point, no tests, and duplicated choreography. The plan's injected
  `HttpClient` seam plus a scripted `FakeClient` gives 13 unit tests over live
  choreography — pagination, settle, review bounding, resume, cleanup — with no
  network. Genuine improvement on the house pattern; keep it.
- **The oracle is server-validated, not hand-drawn.** Task 2 runs each
  `canonical_arguments` through `SetPipelineArgumentsModel` +
  `PolicyCatalogView.for_trained_operator` (reusing parity's exact lookup), and
  Task 3 anchors the stored topology against the server's own args→state
  projection via `build_set_pipeline_candidate`. Two independent checks on the
  thing everything else rests on.
- **Topology comparison as isomorphism** over typed nodes/edges, ids and fork
  labels ignored, option values opt-in via explicit assertions — correct
  semantics, and extracting it from `battery_score` (where the spec put it,
  L731) into its own module is a *better* decomposition than the spec's.
- **Exclusions are first-class, always printed beside `n`**, and
  `unattributed_excess` guarantees no silent gap between floor and observed.
  `--compare` refusing on binding identity is the right instinct — failing loud
  beats a plausible delta.
- **Verdict shape extends rgr's reason-accumulation** (`red_reasons` /
  `green_reasons` / evidence) rather than inventing a fourth idiom. Good.
- **TDD per task, commits by pathspec, whole-tree gates in Task 10.** Correct for
  a shared checkout.

---

## Confidence Assessment

**Overall Confidence: High** (for structure; Moderate for sibling-convention
claims, which come from a delegated read).

| Finding | Confidence | Basis |
|---|---|---|
| 1 (scorer split) | High | Read the full `score_run` body; exclusion block provably scenario-free; both consumer workarounds quoted from the plan |
| 2 (abort conflation) | High | `EXCLUSION_KINDS` and `fire` read directly; the plan's own test demonstrates it |
| 3 (third fork) | High | Server helper signature verified at `_helpers.py:422`; `ComposerToolStatus` verified at `composer_audit.py:41`; spec text quoted; rgr `:524` from delegated read |
| 4 (collection coupling) | High | Module-level `load_scenario` visible at three cited line numbers |
| 5 (module placement) | Moderate-High | Convention ("zero `sys.path` under `tests/unit/evals/`", lib-mirror layout) from delegated read, not re-verified by me |
| 6 (ignore test) | Moderate-High | `test_ignore_policy.py` existence from delegated read; plan's manual-only verification read directly |
| 7 (floor attribution) | High | `_compare` body and `identity.binding` schema read directly |
| 8 (meta triplication) | High | Three definitions located and cited |
| 9 (containment) | High | `RequestsClient`/`step`/`_settle`/`fire` bodies read directly |
| 10 (vocabularies) | High for enums (verified `ComposerPlannerInformationClass`); Moderate for the criteria-drift history (delegated) |

## Risk Assessment

**Implementation Risk: Medium.** **Reversibility: Easy** (delete two
directories + one `.gitignore` hunk).

| Risk | Severity | Likelihood | Mitigation |
|---|---|---|---|
| A multi-hour live round self-terminates on real product signal | High | Likely once grammar drifts | Finding 2 before any live firing |
| Scoring-stack import crashes the live driver mid-round | High | Possible | Finding 1 |
| Silent measurement drift (forked tool-outcome projection, renamed meta key, misspelled criterion) | High | Likely over months | Findings 3, 8, 10 |
| Cross-round comparison attributes a floor revision to the model | High | Possible at first re-baseline | Finding 7 |
| Whole test suite red for the length of Task 3 | Medium | Certain as written | Finding 4 |
| Credential path escapes `.gitignore` after a later edit | High | Unlikely | Finding 6 |

## Information Gaps

1. [ ] Whether `evals/composer-battery/` was considered for `evals/lib/` placement
   in spec review — Decision 6 says "Approach A, new package reusing `evals/lib/`",
   which finding 5 argues is under-applied, but the review panel doc
   (`2026-08-16-composer-battery-design-review.md`) was not read here.
2. [ ] The actual cost of a `--repeats 5` round (provider spend, wall-clock) — it
   determines how expensive an abort (finding 2) really is.
3. [ ] Whether the rgr helpers can be made public without churning three
   dependent harnesses — finding 3(c) assumes it is cheap.
4. [ ] Whether `elspeth-lints` trust-tier rules apply to `evals/` at all (Task 10
   scopes `--root src/elspeth`), which would change the required posture on the
   battery's own Tier-3 boundary (the HTTP client).

## Caveats & Required Follow-ups

**Before relying on this analysis**
- [ ] Re-verify finding 5's convention claims directly (`git grep sys.path
      tests/unit/evals/`, `ls tests/unit/evals/lib/`) — they came from a
      delegated read and drive a structural recommendation.
- [ ] Confirm finding 6 by reading `tests/unit/evals/composer_parity/test_ignore_policy.py`
      before mirroring it.

**Assumptions made**
- Spec rev 4 is authoritative where it and the plan disagree (basis of finding 3).
- The plan's inlined code is the intended implementation, not illustrative.
- Line references are to the plan file at 435a28966.

**Limitations**
- Does NOT cover symbol/path existence beyond the six symbols I grepped
  (reality reviewer), test strategy or coverage adequacy (quality reviewer), or
  second-order effects on the composer's own development loop (systems reviewer).
- Does NOT assess whether the *measurement design* (floors, taxonomy, N=5) is
  statistically sound — that is a spec question, already adjudicated at rev 4.

---

## 2. Quality / test-strategy lens

## Quality Review — Composer Path-Quality Battery Plan

Reviewed: plan `2026-08-17-composer-battery.md` (4081 lines) against spec rev 4 §6. Findings marked **[V]** were verified by executing the plan's own code against the real parity fixture; **[R]** are read-only inferences.

---

### Blocking Issues

**1. [V] Critical — Task 1, `topology_from_pipeline` (plan L241–301): the oracle is blind to whole edges, producing false greens.**
Edges are derived only from an *upstream* node's `on_success`/`on_error`/`routes`/`fork_to`. A coalesce node has none of these — its downstream consumer references it by **node id** (`finalize.input == "merge_results"`). I ran the implementation against `evals/composer-parity/fixtures/fork_coalesce.json`:

```
NODES: source/csv, gate(fork_count=2,route_count=2), coalesce(merge=union,policy=require_all), transform/passthrough, output/json
EDGES: ((0,1,'on_success'), (1,2,'fork'), (3,4,'on_success'))     # node 2→3 MISSING
```

Node 2 has no out-edge and node 3 no in-edge. Three exploits confirmed, all returning `match.ok = True` against the canonical expected topology:
- `finalize.input = "nowhere_at_all"` → **orphaned transform matches** (identical edge tuple).
- `coalesce.branches = {"path_a": "path_a"}` (half-wired fork) → **matches**.
- The two parallel `fork` edges collapse to one via `tuple(sorted(set(edges)))`, contradicting spec §2's "edge **multiset**" rule.

This is the worst failure direction: `wrong_shape` is a *hard* deviation class and the green criterion for every case. Fix — register each node's own `id` as a producer alias so an id-valued `input` yields an edge, and use a real multiset:

```python
# after building tnodes, before edge derivation:
producer_of: dict[str, int] = {}
for i, n in enumerate(nodes_in, start=1):
    if n.get("id") is not None:
        producer_of[str(n["id"])] = i
...
for i, n in enumerate(nodes_in, start=1):
    if n.get("input") is not None and str(n["input"]) in producer_of:
        edges.append((producer_of[str(n["input"])], i, "on_success"))
...
return Topology(tuple(tnodes), tuple(sorted(edges)))      # NOT set(edges)
```
Also add `("branch_count", str(len(branches)))` to `_node_extras` so coalesce arity is typed. **If** `CompositionState.to_dict()` reliably carries `edges` with `from_node`/`to_node`/`edge_type` (the canonical payload does), prefer reading that list on both sides and delete the name-derivation entirely — the plan's "never from the `edges` list" choice is self-inflicted. Add a regression test asserting the fixture's topology has **4** edges and that each of the three exploits above fails.

**2. [V] High — Task 1, `test_swapped_route_wiring_between_two_gates_fails` (L139): the test asserts a failure that cannot occur; it will fail on first run.**
`base` and `swapped` are genuinely isomorphic under the spec's label-ignoring rule — only connection *names* differ. Executed: both yield `((0,1,'on_success'),(1,2,'route'),(1,4,'route'),(2,3,'route'),(2,4,'route'))`; `match.ok = True`. The plan's Step 4 hedge ("if it passes trivially, print both topologies") shows the author suspected this. The danger is an implementer "fixing" the comparator to be label-sensitive, breaking spec §2's core rule. Replace with a genuinely non-isomorphic swap using distinguishable sink plugins:

```python
def test_swapped_route_wiring_between_two_gates_fails() -> None:
    base = {"source": {"plugin": "csv", "on_success": "in", "options": {"path": "r.csv"}},
        "nodes": [
            {"id": "g1", "node_type": "gate", "input": "in", "condition": "True", "routes": {"true": "mid", "false": "csv_out"}},
            {"id": "g2", "node_type": "gate", "input": "mid", "condition": "True", "routes": {"true": "json_out", "false": "jsonl_out"}}],
        "outputs": [{"sink_name": "json_out", "plugin": "json"},
                    {"sink_name": "csv_out", "plugin": "csv"},
                    {"sink_name": "jsonl_out", "plugin": "jsonl"}]}
    swapped = copy.deepcopy(base)
    swapped["nodes"][0]["routes"] = {"true": "mid", "false": "json_out"}   # source-fed gate now feeds json
    swapped["nodes"][1]["routes"] = {"true": "csv_out", "false": "jsonl_out"}
    assert not topologies_match(topology_from_pipeline(base), topology_from_pipeline(swapped)).ok
```
Node multiset is identical; only *which* gate reaches which typed sink differs.

**3. [V] High — Task 1, `topologies_match` (L318–320): `TypeError` on mixed `None`/`str` plugins of the same kind.**
`sorted(n.signature() for n in ...)` compares `(kind, plugin, extras)`; two same-kind nodes where one has `plugin=None` and another a string raise `'<' not supported between instances of 'NoneType' and 'str'`. Reproduced with two `transform` nodes, one plugin-less. Corpus strata `deep_routing`, `error_routing`, `schema_contracts_demo` mix plugin-bearing and plugin-less nodes, so this will crash on real cases — as an exception, not a `MatchResult`. Fix: `key=lambda s: (s[0], s[1] or "", s[2])` on both sorts, and add a test with two same-kind nodes where exactly one has a plugin.

---

### Warnings

**4. [R] Medium — Task 1: `option_assertions` guard is not mutation-tested.** `test_option_assertion_pins_threshold_only_when_listed` (L159) only asserts the *negative* (wrong value fails). An implementation that returned `MatchResult(False, "…threshold…")` whenever `option_assertions` is non-empty passes the whole suite. Add the positive arm: `assert topologies_match(exp, obs, option_values={"gate": {"threshold": 100}}, option_assertions=[("gate","threshold",100)]).ok`.

**5. [R] Medium — Task 1: spec §2 pins coalesce `policy` **and** `merge` exact; only `merge` is tested** (L114). Add the `policy: require_all → first_available` twin.

**6. [V] Medium — Task 2, `test_args_and_state_projections_agree` (L95) is a tautology.** `_as_state` deep-copies `args["nodes"]` verbatim, so both sides traverse the identical node path; it can only exercise `source`→`sources` and `sink_name`→`name` renaming. It cannot detect that the real `CompositionState.to_dict()` node shape differs. The genuine anchor exists — `test_canonical_payload_commits_to_the_expected_topology` (Task 3, L817, via `build_set_pipeline_candidate`) — so this is redundancy, not an unmet spec requirement. Keep it but rename it to `test_source_and_output_key_renaming_projects_identically` so its weakness is legible.

**7. [R] Medium — Task 3, L824: `pytest.skip("inline_blob payloads need a session engine")` is a silent coverage hole.** Spec Decision 9 states canonical payloads use a plain `path` source *precisely so* the anchor is hermetic. A skip means an author who pastes an `inline_blob` payload loses the server anchor silently. Make it an assertion, not a skip:
```python
assert "inline_blob" not in json.dumps(sc.canonical_arguments), \
    f"{case}: Decision 9 requires a plain `path` source in canonical_arguments"
```

**8. [V] Medium — Task 5, `threadgen.audit_row` (L1406/1415): `_T0 = "…00:00:{:02d}Z"` with `seq % 60` makes `wall_ms` non-monotonic.** At `seq=59`, `finished_at` is `:00` — *before* `started_at` `:59` — yielding a negative `wall_ms`; at `seq=60` timestamps silently collide with `seq=0`. No current test passes a seq ≥ 55 (checked), so the suite is green today, but the first implementer adding a longer thread gets a confusingly wrong `wall_ms`. Fix: `_T0 = (datetime(2026,8,17,tzinfo=UTC) + timedelta(seconds=n)).isoformat()`. **Verified correct today:** `ideal_thread` puts audit rows at `:02→:03` and `:07→:08`, so `wall_ms == 6000` (L1525) is right, as are `tokens == {prompt:200, completion:20}`, `cost == 0.02`, and `(tool_bearing, advisor, other_text, audited) == (2,0,0,2)`.

**9. [R] Medium — spec §6 "Discriminator invariants (offline)" partially declined.** Plan L4078 states the advisor-`tools=None` and `composer_advisor_model ≠ composer_model` characterization tests are "existing server-side facts" and are deliberately not added. The routing argument (they belong in `tests/unit/web/composer/`) is defensible, but the plan does not *verify* those tests exist — so the currency discriminator that the entire floor rests on (Decision 8) may be pinned nowhere. Before merge, either cite the existing test ids in the plan or add them. The third invariant (PATCH-before-POST) *is* covered (`test_patch_title_precedes_post_message_and_label_format`, L2792).

**10. [R] Low — Task 3, `test_scenario_is_sound` (L773) does not pin the pre/post-calibration floor record**, which spec §1 calls "mandatory… the only audit trail" for a single-developer instrument. Add `assert sc.floor.pre_calibration == sc.floor.tool_bearing_calls or sc.floor.post_calibration == sc.floor.tool_bearing_calls`, and at freeze assert `post_calibration is not None`.

---

### Strengths worth keeping

- **The taxonomy tests are unusually honest.** Cross-class negatives run *both* directions (`repair`↛`backtrack` L1538, `backtrack`↛`repair` L1570/1579), the near-miss follows the durable pair against a lying envelope stamp (L1553), `cancelled` = not-applied (L1560), and `malformed_output` counts while unrecovered `transport` excludes (L1624/1635). This is exactly spec §6's "both ways + near-miss + cross-class" contract.
- **[V] Pagination boundary is tested on the hard side:** L2846–2858 covers 1003 rows → offsets `[0,500,1000]` *and* exactly 500 rows → offsets `[0,500]` (a full page always triggers a follow-up). That is the spec's named trap, correctly pinned.
- **The advisor bucket test discriminates by model, not by null tools hash** (L1529), including a text-only *composer* row that must not be counted as advisor — the exact inversion that would silently corrupt every floor.
- **Terminal boundaries read the captured body**, not inferred turn counts (L1670), per the spec's `ComposerLLMCallStatus.TIMEOUT` trap.
- **Production readiness of the driver is genuinely planned:** named tests exist for all three abort rules (L2933/2942), `--resume` never re-fetching (L2959), `--cleanup` scoped to this round's complete captures (L2972), the 422 detail body (L2812), the one-shot `composer-progress` read (L2827), and the review loop bound (L2888). Constants are explicit (`CLIENT_TIMEOUT_S=620`, `SETTLE_POLLS=12`, `MAX_REVIEW_ROUNDS=5`).
- Strict TDD shape throughout: every task has an explicit "run tests, expect this failure" step before implementation.

---

## Confidence Assessment

**Overall Confidence:** High for Tasks 1–3 and the scorer arithmetic; Moderate elsewhere.

| Finding | Confidence | Basis |
|---|---|---|
| #1 dropped-edge false green | **High** | Executed plan's code vs real fixture; 3 distinct exploits reproduced |
| #2 swapped-route test fails as written | **High** | Executed; `match.ok = True` against an assertion of `not ok` |
| #3 sort-key `TypeError` | **High** | Reproduced the exception |
| #8 `wall_ms` values in the floor test | **High** | Hand-traced `ideal_thread` sequence numbers; 6000 ms confirmed correct |
| #6 Task 2 tautology | High | Read both projections; `_as_state` copies nodes verbatim |
| #9 declined discriminator invariants | Moderate | Plan states it explicitly; I did not verify the server-side tests exist |
| Report/probe module quality | **Low** | Assessed by test-name matrix only — bodies not read |

## Risk Assessment

**Implementation Risk:** High · **Reversibility:** Easy (pre-implementation; no code exists yet)

| Risk | Severity | Likelihood | Mitigation |
|---|---|---|---|
| Instrument reports false greens; a real `wrong_shape` scores clean, corrupting the frozen `corpus_version: 1` baseline | Critical | Certain if #1 ships | Fix aliasing + multiset before Task 2; add the three exploit regressions |
| Implementer "fixes" the comparator to satisfy #2, breaking label-ignoring isomorphism | High | Likely | Replace the test with the typed-sink version above |
| Crash on real multi-kind corpus cases (#3) during calibration, wasting a live firing | High | Likely | One-line sort-key fix |
| Silent skip (#7) removes the only server anchor for a case | Medium | Possible | Convert skip → assertion |

## Information Gaps

1. [ ] **Bodies of `test_battery_report.py` (L2187–2350) and `test_planner_probe.py` (L3465–3625)** — assessed by name only; the advisor's Σ/Σ concern (does `test_pooled_uses_sum_over_sum_and_excludes_beside_n` use *unequal* per-case `n`? mean-of-means and Σ/Σ coincide at equal `n`, so an equal-`n` fixture cannot fail) is **unverified and should be checked before merge**.
2. [ ] **Whether `CompositionState.to_dict()` carries a usable `edges` list** — decides which of the two fixes for #1 is correct.
3. [ ] **Existence of the server-side advisor discriminator tests** (#9).
4. [ ] **`fake_http.py` (L2648) fidelity** — not read; whether it reproduces FastAPI's `detail` wrapping and 499 semantics is unverified.

## Caveats & Required Follow-ups

**Before relying on this analysis**
- [ ] Re-run my three inline reproductions after the #1 fix; confirm the fixture yields 4 edges and all three exploits fail.
- [ ] Read the report and probe test bodies for the Σ/Σ and information-class-floor guards.

**Assumptions made**
- The parity fixture `fork_coalesce.json` is representative of the canonical payload shape for other coalesce-bearing strata.
- Plan code blocks are intended verbatim (the plan's own "remove the unused `permutations` import" note implies yes).

**Limitations**
- Does NOT cover symbol/path existence (`build_set_pipeline_candidate`, `is_mutation_tool`, `extract_structural_target`) — Reality reviewer's lane.
- Does NOT assess architecture or module boundaries.
- Read-only: no files modified, no HTTP, no LLM, no git writes.

---

**Most impactful remaining work (checkpoint):**
- Read `test_battery_report.py` bodies to settle the Σ/Σ pooling mutation-gap (gap #1 above) — highest remaining yield.
- Read `fake_http.py` + `test_planner_probe.py` for probe/tripwire honesty (spec §6's "`undetermined` is a flag, never a pass").
- Verify whether `CompositionState.to_dict()` exposes `edges`, which selects the correct fix for finding #1.

---

## 3. Reality lens (symbols, routes, wire shapes)

# Reality check — `2026-08-17-composer-battery.md` (plan @ 435a28966)

Status: **PARTIAL** — Batch 1 (import-answerable symbols) and part of Batch 2
(route surface) complete. Remaining scope listed at the end.
All claims below were settled by **executing** imports in the venv, or by
reading the route/schema source — not by grep-for-symbol.

## Verified (VERIFIED)

| # | Claim | Evidence |
| --- | --- | --- |
| 1 | All 17 names in `SPEC_MUTATION_VOCAB` are mutation tools | `is_mutation_tool` → True for all 17; `src/elspeth/web/composer/tools/discovery.py` |
| 2 | `get_pipeline_state`, `list_models`, `get_expression_grammar`, `list_recipes`, `get_audit_info` are discovery tools | all `is_discovery_tool` → True, `is_mutation_tool` → False |
| 3 | `is_mutation_tool("apply_pipeline_recipe")` is True (Task 5 note) | True |
| 4 | `PIPELINE_STAGED_*` — exactly the five the plan names | `elspeth/web/composer/protocol.py` |
| 5 | `ComposerToolStatus` = SUCCESS/ARG_ERROR/CANCELLED/PLUGIN_CRASH → `_FAILED_STATUSES={"arg_error","plugin_crash"}` and `"cancelled"` correct | `contracts/composer_audit.py` |
| 6 | `_TRANSPORT_STATUSES` / `_MALFORMED_STATUSES` literals all exist | `ComposerLLMCallStatus` = success/timeout/api_error/auth_error/bad_request_error/malformed_response/cancelled, `contracts/composer_llm_audit.py:39` |
| 7 | `ComposerPlannerCode` covers every triage key incl. six `*_EXHAUSTED` | `contracts/composer_planner_audit.py` |
| 8 | `ComposerPlannerAttempt.to_dict()` keys ⊇ plan's `PlannerAttempt` fields | to_dict source read; adds only `candidate_shape_hash` |
| 9 | Every `LOOP_TOOL_TO_INFO` value is a real `ComposerPlannerInformationClass` | enum dumped; `plugin.schema`, `model.catalog`, `catalog.selection`, `pipeline.current`, `recipe.index`, `audit.info`, `expression.grammar`, `plugin.assistance` all present |
| 10 | `LlmCall` parser field names all real on `ComposerLLMCall` | dataclass fields read |
| 11 | `PASSIVITY_PHRASES` / `BUILD_FAILURE_SENTINELS` exist **and are lowercase** (scorer lowercases before `in`) | `evals/lib/scenario_from_example.py` |
| 12 | `extract_structural_target(dir, variant)` signature + keys `gates`/`coalesce_nodes`/`aggregations`/`transforms` | executed on `examples/fork_coalesce` |
| 13 | `SetPipelineArgumentsModel` fields = source/sources/nodes/edges/outputs/metadata | `model_fields` |
| 14 | `PolicyCatalogView.for_trained_operator(full, snapshot)`, `PluginAvailabilitySnapshot.for_trained_operator(catalog)`, `create_catalog_service()`, `PluginId(kind,name)` (PluginKind is a `Literal`, so plain strings work) | signatures |
| 15 | `validate_canonical_arguments` mirrors parity `_referenced_plugins` exactly | `tests/unit/evals/composer_parity/test_fixtures.py` |
| 16 | `CompositionState(nodes=(),edges=(),outputs=(),metadata=PipelineMetadata(),version=1)` constructs; `PipelineMetadata()` no-arg works | executed |
| 17 | `ToolContext(catalog=…, plugin_snapshot=…)` minimal construction works | both required, all else defaulted |
| 18 | `build_set_pipeline_candidate(args, state, ctx)` → `.result.success` and `.result.updated_state` both real | `ToolResult` fields; **executed** on fork_coalesce + linear_transform, both `success=True` |
| 19 | `CompositionState.to_dict()` → `sources`/`nodes`/`edges`/`outputs`, `outputs[].name` | executed |
| 20 | Gate `routes` value `"fork"` really is the sentinel the comparator skips | committed fork_coalesce gate = `routes:{"true":"fork","false":"fork"}, fork_to:[…]` |
| 21 | 10 parity fixtures exist, all carry `intent` + `canonical_arguments`; `len(PROBE_FIXTURES)==10` holds | glob + key check |
| 22 | Probe pairing claim (plan L3885): all 10 intents → EXPLICIT_MUTATION, all `"Hi. "+intent` → AMBIGUOUS | executed against the live grammar |
| 23 | `GET /messages` is a **bare list** (`response_model=list[ChatMessageResponse]`) | `sessions/routes/messages.py:991-993` |
| 24 | Query params `limit` (ge=1, **le=500**), `offset` (ge=0), `include_llm_audit`, `include_raw_content`, `include_tool_rows` all exist | `messages.py:999-1006` |
| 25 | `ChatMessageResponse` fields exactly match the plan's synthetic fixtures | `sessions/schemas.py:184-195` |

## WRONG

### F1 (BLOCKING) — `PipelineMutationIntent` does not exist
`elspeth.web.composer.no_tool_policy` exports **`PipelineMutationIntentDecision`**
(members `EXPLICIT_MUTATION`, `CONVERSATIONAL`, `AMBIGUOUS`). There is no
`PipelineMutationIntent`.

The plan uses **both names**, so Task 3 is right and Task 8 is wrong:
- Plan L744 (Task 3 test) imports `PipelineMutationIntentDecision` — **correct**.
- Plan L3447 (Task 8 interface line) and **L3648** (`planner_probe.py` import) and
  **L3699** (`PipelineMutationIntent.EXPLICIT_MUTATION`) — **ImportError at module load**.

Fix: in Task 8 replace `PipelineMutationIntent` with
`PipelineMutationIntentDecision` at all three sites. `classify_pipeline_mutation_intent(message: str) -> PipelineMutationIntentDecision`
returns the enum directly (no wrapper), so `is`-comparison is correct.

### F2 — `create_blob`/`update_blob`/`delete_blob` are MUTATION tools, not discovery
`is_discovery_tool` → False, `is_mutation_tool` → **True** for all three.
Consequence: in `battery_score.score_run` the clause
`if is_discovery_tool(name) and name not in _DATA_TOOLS:` (plan L2004) has a
**dead `_DATA_TOOLS` guard** — blobs never reach it. Behaviour is still correct
by luck, because the explicit `create_blob` / `update_blob|delete_blob` branches
(L2010-2016) run *before* the `if not is_mutation_tool(name): continue` gate.
Fix: drop `and name not in _DATA_TOOLS` and say plainly that blob tools are
mutations handled ahead of the generic mutation path — otherwise a later reader
reorders the branches and silently reclassifies every data detour as a mutation.

### F3 — `ComposerLLMCallStatus` is in the wrong module for the plan's verification command
Plan L1346 tells the implementer to check statuses with
`git grep -n "class ComposerToolStatus" -A12 -- src/elspeth/contracts/`.
That finds `ComposerToolStatus` (composer_audit.py) but the LLM-call statuses the
scorer keys on live in **`src/elspeth/contracts/composer_llm_audit.py:39`**
(`ComposerLLMCallStatus`). Both literal sets happen to be right; the instruction
would only verify half of them. Add the second grep.

### F4 — synthetic fixture emits an invalid `provider_cost_source`
Plan L1068/L1073/L1074 and `threadgen.audit_row` (L1416) set
`"provider_cost_source": "provider"`. The real vocabulary is
`not_available` | `response_usage.cost` | `_hidden_params.response_cost`
(`composer_llm_audit.py:23-31`), enforced in `ComposerLLMCall.__post_init__`.
The fixture is raw JSON so nothing raises, but it is not the shape the server
emits and the plan bills it as "shape-faithful". Use `"response_usage.cost"`.

### F5 — fake `POST /messages` body uses a key the server does not return
Plan L2731 returns `{"message": {}, "new_state": None}`. The real response model
is `MessageWithStateResponse` = `message` / **`state`** / `proposals`
(`sessions/schemas.py:198-207`). Harmless today (the driver ignores the 200 body)
but it teaches the wrong shape to anyone extending the fake.

## Remaining scope (NOT yet verified)
422 body wrapping; `_tool_call_outcomes_by_call_id` precedence diff; audit
envelope `_kind`/`role=="audit"`; `POST /api/sessions` `{}`→201; preferences
PATCH literals; interpretations/resolve; `/state`; `/validate?state_id`;
`/proposals`; `/api/system/status` `version`; `is_default_session_title`
auto-title guard; `deploy/elspeth-web.env` names; `.gitignore` block;
`sys.path`/`evals` importability under pytest.

---

## 4. Systems lens (second-order effects)

**Checkpoint note:** I hit the usage limit before the Task 3/Task 8 sub-read returned; findings below cover Tasks 1–7 + 9–10 read directly, and I flag the probe/tripwire coverage gap explicitly.

# Systems Review — Composer Path-Quality Battery Plan

Reviewed: `docs/superpowers/plans/2026-08-17-composer-battery.md` @ `435a28966` against spec rev 4 and the review record. Read-only; nothing fired, no file modified.

## Findings

**1. (High) Floors and scenarios are late-bound; `--compare`'s version guard is defeatable.**
`report.py` loads `corpus_version` and every `scenario.json` from the **current tree** (plan 2581-2582) and stamps the report with it (2476); `score_run` never reads `meta.json.corpus_version` or `prompt_sha256` (1941-1947). So re-scoring a round captured at v1 after a floor widening produces a report labelled v2, and `_compare`'s guard (2490) compares two freshly-regenerated reports that both claim v2. Mechanism: the instrument's own history silently improves when its floors move — the exact Goodhart loop §1's widening rule was written to prevent. **Mitigation:** in `collect_scores`, assert each run's `meta.corpus_version` == the loaded version and `meta.prompt_sha256` == the scenario's prompt hash; refuse otherwise. Record `floor` and `scenario_sha256` in `score.json` (floor is already there — add the hash).

**2. (High) `composer_skill_hash` is hardcoded `None`, so the independent variable under test is never recorded.**
`_identity` (3283) emits `"composer_skill_hash": None` unconditionally; the spec makes it the *recorded* delta a kit-edit comparison is read on, and `render_markdown` prints it first (2524). Mechanism: compare two rounds across a `pipeline_composer.md` edit and the header reads "Recorded deltas: none" — the report affirmatively implies the same kit. **Mitigation:** populate from `GET …/interpretations` when a review exists; when null, append a degraded reason ("kit under test unrecorded") rather than printing a bare `None`.

**3. (High) Binding identity fails open: absent fields become `None` on both sides and `--compare` passes.**
Every binding field is `st.get(...)`/`ft.get(...)` with no non-null assertion (3282); `_compare` refuses only on inequality (2493-2495). If `/api/system/status` stops exposing `composer_model`, or no tool-bearing row was captured, both rounds record `None` and the guard is satisfied. Same class: budgets come from the **local** `deploy/elspeth-web.env` (3402), not the running server — a server-side budget change is invisible to the guard and its effect lands as a "kit" delta. **Mitigation:** hard-fail the run when any binding field is null; record the env file's SHA and mtime, and treat "budgets are operator-asserted, not observed" as a printed caveat.

**4. (High) `meta.json` records intended preferences as though applied.**
`step("patch_preferences", …)` return is discarded (3187) and `_write_meta` stores `PINNED_PREFERENCES` verbatim (3287). The route exists (`sessions/routes/composer/state.py:372`), but a 404/422/429 leaves the session on the account's actual `trust_mode` while the artifact asserts `auto_commit`. Under `explicit_approve` every run fires `approval_pending` (hard) and the whole firing reads as a composer regression. Same pattern for `patch_title` (the PATCH-before-POST *contract* is unit-tested for ordering, never for success). **Mitigation:** read preferences back and store the response; fail the run on mismatch.

**5. (High) Transport exclusion outranks the terminal classes, and `cancelled` is a transport status — the worst runs leave the denominator.**
`_TRANSPORT_STATUSES` includes `cancelled` (1784, deviation (c)); `unrecovered` = a transport row with no *later* success (1955-1956); exclusion precedence puts `transport` above everything terminal (2113). `ComposerLLMCallStatus.CANCELLED` is exactly what `audit.py:1079-1094` records when the coordinator cancels in flight — i.e. on wall-timeout/turn-exhaustion shutdown, where the cancelled row is naturally *last*. Mechanism: budget-exhaustion runs get excluded as `instrument_error: transport` instead of counted as hard `wall_timeout`/`turn_exhaustion`, inflating clean/optimal and deflating hard. **Mitigation:** when `server_terminal.budget_exhausted` is present, the terminal class wins over `transport`; and drop `cancelled` from transport (or gate it on "no server terminal reason").

**6. (Medium-High) `retried_provider_error` puts provider weather in the headline clean rate.**
`clean` requires zero deviations (2120), and `retried` fires on *any* transport row with *any* later success anywhere in the run — not a retry of the same call. A round fired during OpenRouter congestion has a systematically lower clean rate than one fired quiet, and `--compare` will attribute that to the kit. **Mitigation:** report `clean` and `clean_excluding_transport` side by side, and add a degraded reason when pooled `retried_calls` exceed a stated threshold.

**7. (Medium-High) Deviation (d) routes a real behaviour into the class most likely to be answered by widening the floor.**
`seen_discovery.clear()` on any applied mutation (2041) means post-mutation schema re-reads produce excess with no class → `unattributed_excess`, which the plan grades **soft** (1781) and the report folds into the per-case histogram only — it is not in the headline and not a degraded trigger. Spec's remedy for a high `unattributed_excess` rate is "candidate mis-derived floor… re-reviewed", i.e. the natural fix is to widen the floor and the signal disappears. **Mitigation:** headline `unattributed_excess` with `n`; make a pooled rate over ~15 % a degraded reason; keep the class severity-neutral rather than soft.

**8. (Medium) A rejected mutation that is never retried produces no ledger evidence, and is labelled `decline`.**
`pending_failed` is only flushed when a *subsequent* mutation appears (2020-2022); a dangling one is dropped, discarding the rejection `codes` — the single most useful triage datum. The run then falls to `passivity if phrase_hits else decline` (2074), so a failed build is filed as a refusal. **Mitigation:** flush `pending_failed` at end-of-events as `repair` (or a new `abandoned_mutation`), and suppress `decline` when any mutation was attempted.

**9. (Medium) Below-floor runs are invisible.** `excess = max(0, …)` (2092); a run under floor is `clean` but not `optimal`, with no signal that the floor is too high. Add a `below_floor` flag — floors are currently only checked from above.

**10. (Medium) Capture is fail-open in two places that matter.** A timeout on `list_reviews` yields `events=[]` → `exhausted=False` → the loop exits as if no reviews were pending (3207-3211), and the run can still score clean with reviews unresolved. `_settle` bypasses `step()` and calls `client.request` directly (3259), so an `HttpTimeout` there propagates out of `run_prompt` and kills the firing — violating spec §4's "capture failure never kills the round". `step()` also catches only `HttpTimeout`, not connection errors: a live redeploy mid-firing aborts the round with a traceback. **Mitigation:** route every call through `step()`; treat a null review listing as `instrument_error`, not "none pending".

**11. (Medium) Rate limiting is unhandled and forms a failure amplifier.** `composer_rate_limit_per_minute=10` (`deploy/elspeth-web.env:13`) applies to `POST /messages` only, so serial firing is normally safe — but fast-failing runs (immediate 422) raise the POST rate, 429s are classified as generic `http`/`terminal_missing`, and three in a row trip the abort with a misattributed reason. **Mitigation:** classify 429 explicitly with evidence, and enforce a ≥7 s minimum inter-run spacing.

**12. (Medium) `--cases` skips the canary and the report still reads healthy.** `fire` only runs the canary if it is in `selected` (3315); with `n=0`, `canary.flag` is `False` (2439) and the header prints `flag=False`. Make `n < CANARY_N` a degraded reason.

**13. (Medium) `--compare`'s refusal has no release valve.** Any binding drift — including an OpenRouter `model_returned` repoint — makes comparison impossible with no `--force-compare`. The predictable workaround is hand-editing `meta.json`, which destroys the immutable-capture property the whole design rests on. Add a forced mode that stamps a loud caveat.

**14. (Medium) Ordering has one real executor stall and one soft one.** Task 7's `main` imports `run_probe`/`run_tripwire` from Task 8 (3397) and `fire()` takes a `tripwire` callable — so Task 7's CLI is not runnable until Task 8 lands, and `_verdict` (3291) needs real `scenario.json` files from Task 3. The plan's Task order is 3 → 7 → 8, so this holds, but Task 7's *step 5* test run must not exercise `main()`. Task 6's canary handling likewise needs the canary scenario authored in Task 3.

**15. (Low, but expensive later) Deviations are counted in a different currency than excess.** Cached discovery repeats (`is_cacheable_discovery_tool`) cost no provider call yet fire `excess_discovery`, so classes can fire at zero excess and excess can occur at zero classes. The report should not be read as "the histogram explains the excess"; say so in `CAVEATS`.

## What the plan gets right systemically

Offline scoring over immutable capture with the driver forbidden from scoring for measurement (3010-3011) is the correct seam, and it makes taxonomy revision cheap. Exclusions leave the denominator *and* flag the firing, with evidence strings attached. `unattributed_excess` exists at all. Pooled Σ/Σ with `n`, exclusions, and MDE beside every rate, per-case CI labelled indicative, and per-repeat bins with cached-token medians are honest statistics for N=5. Round-robin firing with a per-case streak flag correctly prevents one drifting case from tripping the global abort. `--resume` never re-fetching preserves capture immutability. `--cleanup` is gated on both title prefix *and* capture completeness, and defaults off. Deviations from spec text are declared rather than smuggled — items (a), (e), (f) are sound as written; (b), (c), (d) are the three that change what the instrument measures (findings 5, 7, and the review-exhaustion→`http` mislabel in 10).

## Confidence Assessment

**Overall: Moderate-High.**

| Finding | Confidence | Basis |
|---|---|---|
| 1, 2, 3, 4, 9, 12, 14 | High | Read directly from the plan's own code blocks with line numbers |
| 5 | Moderate-High | Enum + `audit.py:1079-1094` cancellation path read; not observed on a live wall-timeout run |
| 6, 7, 8, 13, 15 | Moderate | Mechanism traced statically; magnitude depends on real firing rates |
| 11 | Moderate | Limit value and limited route verified; the amplification path is inferred |

## Risk Assessment

**Implementation risk: Medium. Reversibility: Easy for code, Difficult for data.** Every finding is a cheap edit *now*. What is expensive later is the corpus and its floors: once `corpus_version: 1` is frozen and rounds are captured, changing a floor invalidates comparability, and finding 1 means it does so *silently*. Fix 1, 2, 3 before the calibration firing; the rest can land after.

## Information Gaps

1. [ ] Task 3's classifier-gate test and Task 8's probe/tripwire were dispatched to a sub-read that did not return — the probe-arm "classifier fingerprint" guard, the tool-vocabulary registry pin, and the `.gitignore` re-include are **unreviewed here**.
2. [ ] Whether `PATCH /sessions/{id}/composer/preferences` accepts `trust_mode`/`density_default` in that shape (route exists; body unverified).
3. [ ] Observed canary optimal rate — finding 12's alarm-fatigue risk cannot be sized without it.

## Caveats & Required Follow-ups

- Re-run the Task 3/Task 8 lens before merge; drift risks (classifier grammar, `PIPELINE_STAGED_*` copy, threadgen-vs-wire divergence) live mostly there.
- I did not verify symbol existence, test coverage, or security — other lenses own those.
- Assumption: `CANCELLED` audit rows appear on coordinator-cancelled compose shutdown. If a live wall-timeout run shows otherwise, finding 5 drops to Low.
