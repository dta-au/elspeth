# Composer battery — final whole-branch review (68d508965..cac244853)

Reviewer: Senior Code Reviewer (single seat, no subagents). Read-only on `/home/john/elspeth`; nothing fired at a live host.

**How this was read.** In passes, by module rather than by diff hunk (every file in the range is new, so the tree at HEAD *is* the diff): (1) constraints, ledger, triage list, spec rev 4 + errata, plan structure; (2) `battery_topology` → `battery_scenario`/`battery_corpus` → `battery_capture` → `battery_score` → `battery_report` → `battery_planner`; (3) `drive_battery.py`, `report.py`, `planner_probe.py`; (4) tests (`threadgen`, `fake_http`, the eight test modules, the advisor discriminator test), `corpus.md`, three scenario JSONs in full, README + calibration record, `.gitignore`/`pyproject`/conftest/hint; (5) the ELSPETH seams the driver and capture layer assume — `routes/messages.py` (`get_messages` flags, `limit ≤ 500`, offset paging), `_helpers.py:_tool_call_outcomes_by_call_id` + `_message_response` (wire shape), `_composer_conversation_tool_or_llm_audit_messages`, `composer/audit.py` public llm-call fields, `composer/state.py:get_current_state` (200 + `null` when no state), `execution/routes.py` validate, `interpretation.py` list/resolve, `composer/proposals.py`, `sessions.py:list_sessions` (`limit ≤ 200`, default 50), `app.py:system_status` keys, `_auto_title.py` guard, `tools/_registry` name sets, `contracts/composer_progress.py` reason vocabulary, the 422 detail builder. (6) Ran the 176 battery tests + advisor test (`-n 0`, green), `ruff check`/`format --check` (clean), a `getattr`/`hasattr` grep over the added lines (none), a per-commit path audit (only battery paths + the named test file; nothing under `src/`), and ~12 offline adversarial probes against the scorer and the driver through `FakeClient` — the concrete outputs are cited inline below.

---

## Strengths

- **The instrument's spine is right and well-anchored.** Currency (success + non-null `tools_spec_hash`, advisor by model), the two-half `score_path`/`judge` split, the durable-pair outcome projection (verified branch-for-branch against `_helpers.py:422-486`), the isomorphism oracle with `condition_literal` membership, the late-binding guard, `--compare` refusal with recorded deltas (skill hash first), canary-first + `should_abort` on instrument kinds only, PATCH-before-POST, offset pagination with "a full page always fetches again", login-only/never-register, never `/execute`, never cache an error body — all present and pinned by tests that mutate the guard, not the symptom.
- **Every scenario self-matches, including the three option assertions** (I ran `topologies_match` over all 19 payloads with each scenario's own `option_assertions`: 19/19 `ok`). The server-anchor test (`build_set_pipeline_candidate` → `to_dict()` → topology ≡ stored) is the strongest possible hermetic oracle for the projection.
- **The taxonomy tests are genuinely both-ways** with near-miss (lying stamp vs durable pair), cross-class negatives, boundary-on-captured-body (`turns_used` 39/40, provider `TIMEOUT` ≠ `wall_timeout`, cancelled last row on a wall timeout stays hard), and the wire-shape builder (`threadgen`) mirrors `ChatMessageResponse` field-for-field.
- **Honest reporting posture:** `n` + exclusions + formula beside every rate, instrument/measurement exclusion split rendered separately, `unattributed_excess` and `below_floor` on their own headline line, `ci_half_width_pp` labelled as what it is, `FORCED COMPARE` stamped into caveats, `findings` distinct from `degraded`.
- **Discipline held under a shared checkout:** every commit is pathspec-scoped to battery paths; the one `src/`-adjacent addition is the named test file; ruff-clean; no dynamic-attribute sites; the sibling's `sys.path` hint was captured into `recent-code-hints.md`.
- The prompt corpus reads as operator voice, states thresholds/`k` where the stratum needs them, and every prompt is classifier-gated in CI with the recorded decision pinned.

---

## Issues

### Critical (Must Fix)

**C1. A completed, valid, at-floor run is scored NOT CLEAN / NOT OPTIMAL if its closing sentence contains an RGR "passivity" phrase — with no deviation and no ledger entry.**
`evals/lib/battery_score.py:584` appends a red reason for `passivity_hits` unconditionally, and `:599` folds red into green (`green = not green_reasons and not red_reasons`); `clean` requires `green`.
Probe (ideal `fork_coalesce` thread, final content `"Pipeline built and validated. Let me know if you want any changes."`): `clean=False optimal=False green=False red=True deviations=[] red_reasons=["forbidden passivity phrases in final message: ['let me know if']"]`.
Why it matters: the phrase list (`'let me know if'`, `'should i '`, `'would you like me to'`, …) is a normal LLM closing on a *finished* build; spec §3 defines `decline`/`passivity` as "**no mutation** + permission-seeking (RGR red criteria reused)" and spec §3's clean = "zero deviation events ∧ green criteria ∧ is_valid" — red is not in the definition. As implemented the headline clean/optimal rates can be depressed by an arbitrary fraction with nothing in the ledger to explain it (the report's `hard` count also excludes these runs, so the three headline numbers stop reconciling).
Fix: gate the passivity red reason on `not path.applied_any` (keep `build_failure_sentinels`, `state_empty`, `is_valid is False` unconditional); keep `red`/`red_reasons` reported. Add the probe above as a test (`test_courteous_closing_after_an_applied_valid_mutation_is_still_clean`). Also see I3 (ledger evidence for criterion-only non-clean runs).

**C2. Capture-step server failures land in the denominator or in the measurement bucket instead of being instrument exclusions.** Three concrete shapes, all confirmed through `FakeClient` + `score_from_disk`:
- `POST /validate` returns 5xx → `drive_battery.py:354` writes no `validate.json` and sets nothing on `instrument`; the run scores `excluded=None, clean=False, green_reasons=['not is_valid','topology: no valid state'], deviations=[]` — a validate outage becomes a product failure with no ledger evidence.
- `GET /state` returns 5xx → `:350-353` silently skips (`state.json` absent) → `excluded=None, red=['final composition state is null or structurally empty']` — same class of error. (The legit no-state case is HTTP 200 + `null` body per `composer/state.py:456-458`; only non-200 is a fault.)
- `POST /messages` returns 5xx **before any provider call** (server crash-loop / `service_setup_failed`) → zero audit rows → `battery_score.py:470` `surface != "compose_loop"` fires *before* `:480` `terminal_missing`, so the verdict is `surface` (a MEASUREMENT kind): it never feeds `should_abort`, never counts toward the instrument 15 % flag, and renders under "Measurement exclusions (product findings)". A dead substrate burns a full 95-run round and the report calls it 95 product findings.
Spec §4: "a run missing any artifact is `instrument_error: capture`"; abort rule (a) exists exactly for the third shape.
Fix: (a) driver — on `get_state`/`validate` non-200 set `instrument["http_unrecovered"] = instrument["http_unrecovered"] or f"{step} {status}"`; on `post_message` status ≥ 500 likewise (a 5xx is a server fault by construction — the composer's structured terminal is 422); (b) scorer — for `surface == "undetermined"` with `post_status != 200`, classify as `terminal_missing`/`http` (instrument) ahead of `surface`; keep `surface` for planner rows and for a 200 with zero rows. Tests for all three shapes.

### Important (Should Fix)

**I1. `.gitignore` re-include does not mirror composer-parity's credential re-exclusions** (`.gitignore:81-92` vs `:63-80`; Global Constraint "mirrors composer-parity's credential re-exclusions"). The battery `!/evals/composer-battery/**` undoes the repo-wide defence-in-depth rules at `.gitignore:18-28` and re-adds only 7 patterns. Verified with `git check-ignore`: `evals/composer-battery/jwt-1.txt`, `x.bearer_token`, `login_x.json`, `x.p12`, `.env.staging` are all **TRACKABLE** (the parity equivalents are ignored). Fix: copy parity's full block (`jwt-*.txt`, `jwt_*.txt`, `*.bearer_token`, `login_*.json`, `*.p12`, `*.pfx`, `.env.*` + the `.env.example/.template/.sample` re-includes) and extend `test_ignore_policy.py`'s parametrize with those paths. (This is a plan-fence defect — L824 was partial — not an implementer slip.)

**I2. The tripwire runs outside containment and outside `--resume`.** `drive_battery.py:581-582` calls `tripwire(self)` bare; `planner_probe.py:27-40` calls `battery.run_prompt` directly (not `_contained`/`_run_or_resume`) and `battery_planner.py:162` `score_arm` calls `load_capture` (raises `CaptureError`). Consequences: (a) any exception in a tripwire run or a malformed tripwire capture propagates out of `fire()` — the round dies with a traceback after the canary's 10 runs (spec §4: "a multi-hour round never dies on one traceback"); (b) under `--resume` the tripwire re-fires and **overwrites** `_tripwire/<fixture>/1` (spec §4: `--resume` "never re-fetches or overwrites a captured page"); (c) `assert_pair_routes` (classifier drift) raises at tripwire time, i.e. after the canary was spent, rather than as a pre-flight. Fix: wrap the tripwire call in `fire()` with the same containment as a run (record `firing["tripwire_error"]`, continue), skip fixtures whose dir `run_dir_is_complete` when `resume`, make `score_tripwire_dir` map `CaptureError` → `pass: False, reason: "capture: …"`, and run `assert_pair_routes` for all `TRIPWIRE_FIXTURES` before the canary. Same containment for `run_probe` (a crash on arm 14 of 20 loses the scoring of the 13 already fired — and there is no re-score entry point, see M12).

**I3. A non-clean run with no deviation is invisible in the report.** `battery_report.py:236-249` builds the ledger from `deviations` only; `red_reasons`/`green_reasons` (schema-before-first-mutation, `is_valid` false, state empty, sentinels, C1's phrases) never reach `report.md`. Per-case `clean` drops with an empty histogram. Spec success criterion (a): "every deviation it reports carries evidence a reader can act on without opening a raw transcript." Fix: add a "criteria" ledger section (case/repeat → reasons) or emit a pseudo-class per failed criterion; assert it renders.

**I4. `path_from_disk` / `score_from_disk` are documented "Never raises" but raise on a messages list of non-dicts.** `battery_capture.py:144` only checks the container; `load_capture`'s sort key does `m.get(...)`. Probe: `messages.json = [1,2,3]` → `AttributeError: 'int' object has no attribute 'get'` out of `path_from_disk`. On the driver side that escapes `_contained` (it re-calls `path_from_disk` in its handler); on the report side it kills `collect_scores` for the whole round. Low likelihood (the server returns dicts) but this is the containment seam. Fix: in `load_capture`, `if not all(isinstance(m, dict) for m in messages): raise CaptureError(...)`; and have `_contained` tolerate a second failure (write the fallback verdict `"capture"` without re-scoring).

**I5. `--cleanup` only sees the first 50 sessions.** `drive_battery.py:614` `GET /api/sessions` with no `limit`/`offset`; the route defaults `limit=50, le=200` (`sessions.py:485`). A 95-run round + tripwire leaves ≥ 45 sessions undeleted, silently. Fix: paginate (`limit=200`, `offset` until short page). Also `_tripwire`/`_probe` titles have three path segments after the prefix, so `len(rest) != 2` skips them forever — decide whether that is intended and say so in the README.

**I6. `--cases` with an unknown name is silently dropped** (`drive_battery.py:568`): `--cases fork_coalese` fires nothing and exits 0. Fix: `unknown = only - set(cases)` → exit 64 with the names.

### Minor (Nice to Have)

- **M1** `drive_battery.py:311` maps only `convergence_wall_clock_timeout` from `composer-progress`; `convergence_composition_budget`/`convergence_discovery_budget` should map to `composition`/`discovery` too, and when `reason` is `None` (progress endpoint non-200 / no snapshot) `source` should be `"none"`, not `"composer_progress"` — the scorer's `terminal_missing` keys on `source`.
- **M2** `_identity` records `server_version = st.get("version")` but `/api/system/status` (`app.py:1786-1804`) has no `version` key → always `null` (rendered in `report.md`). Drop it or source it from somewhere real.
- **M3** `battery_score.py:98-102` comment says the four data tools "manage blob-store content, not CompositionState" — false for `wire_blob_inline_ref` (it patches a source/node/output option and bumps the state version; the registry excludes it from `_BLOB_STORE_ONLY_MUTATION_TOOL_NAMES`). The scoring convention is fine; the comment should say "treated as the bind half of the detour by convention".
- **M4** `scenario.red_criteria` (`passivity_phrases: "rgr_default"`, `build_failure_sentinels: [...]`) is loaded and validated but never read by `judge`, which uses the RGR module constants directly. Either read them or document that the block is declarative.
- **M5** `Score.scenario_sha256` comment (`battery_score.py:186`) says "report checks it" — nothing does. Also `_floors_sha` (`battery_report.py:152`) hashes floor + option_assertions only; an `expected_topology` edit without a version bump would silently move `wrong_shape` across rounds. Include `expected_topology` in the binding hash.
- **M6** `case_flags` in `firing.json` has no reader; the report re-derives the per-case streak and the 15 % flag from bytes (better), but the plan text and README claim the report renders it — correct the prose (or render `firing.case_flags` beside `degraded`).
- **M7** conftest `sys.path.insert(0, …)` → `append` per the sibling's hint (02b2a4291); `report.py` in that dir is a generic name.
- **M8** `test_ignore_policy.py` and README: list `--base/--env-file/--state-dir/--runs-dir` (README omits them) and the fact that `--cases canary` still fires the tripwire.
- **M9** `fake_http.py:64-66` returns 404 for "no state"; the real route returns 200 + `null` — align the fake so a future driver edit is exercised against the wire.
- **M10** `no_calls` (measurement) swallows a run whose every provider call was `malformed_response` (no *successful* tool-bearing row) — spec §3 says `malformed_output` is hard and counted. Spec conflation; note in errata or route "all calls malformed" to hard.
- **M11** `load_corpus` builds a dict from `parse_corpus`'s list — a duplicated `## case` heading silently keeps the last prompt. One-line guard.
- **M12** No re-score entry point for `_probe`/`_tripwire` (`score_probe_dir`/`score_tripwire_dir` are library-only); add `--probe-score-only`/`--tripwire-score-only` or a README one-liner.
- **M13** README "Layout" row `../lib/battery_*.py` omits `battery_planner`/`battery_corpus`; `battery_planner.py` docstring "~25 lines" is stale.

---

## Triage of the ledger's deferred minors and rulings

Deferred minors — **[must fix before merge | may follow | not worth doing]**:

- T1 `if conn == "fork": continue` untested/uncommented — **may follow** (it is reachable via fork_coalesce's `routes.true = "fork"`; add the comment now, a test later).
- T1 `_MISSING`/`_sig_key` defined after first use — **not worth doing** (works; style).
- T1 `observed_option_values` no direct test — **may follow** (covered indirectly by the condition_literal tests).
- T2 `load_scenario` doesn't require ALL closed-vocab keys present — **may follow** (`judge` defaults absent keys to True; a missing key silently enables a gate — worth a one-line "all GREEN_KEYS present" assertion, low risk).
- T3a `_HEADING` lowercase-only uncommented — **not worth doing**.
- T4 `parse_instrument` doesn't type-check `read_integrity`/`http_unrecovered` — **may follow**.
- T4 `llm_calls` coerces missing timestamps to `"None"` — **not worth doing** (`_parse_ts` drops them; `wall_ms` degrades to 0).
- T5 docstring overclaim ("silence is not an option") — **must fix before merge**, together with C1/I3, because it is currently *untrue* (criterion-only non-clean runs are silent).
- T5 `option_assertions_hold` dead switch — **may follow** (document that it is folded into `topology_matches_expected`).
- T5 auth evidence prints post_message status even when auth came from elsewhere — **may follow**.
- T5 transport gated on `budget is None` — **agree with the implementation as-is** (a captured turn/wall budget outranks a dead last row; a non-budget server terminal with an unrecovered transport row IS an instrument fault). Not a change.
- T5 cost sum has no `unknown_cost_calls` counterpart — **may follow**.
- T5 missing `advisor_model` ⇒ advisor calls land in `other_text_calls` — **may follow** (`read_env_budgets` hard-fails when the env var is absent, so the driver always populates it; the report caveat says it is operator-asserted). Add a `binding.advisor_model is None ⇒ degraded` reason later.
- T5 `path_from_disk` catches only `CaptureError` — **must fix before merge** (= I4).
- T5 `data_rework` fires on any `update/delete_blob` — **may follow** (spec says "same invented data"; a second blob for genuinely different data would misfire — calibration will show whether it ever happens).
- T5 `pytest.raises(Exception, match="instrument")` → `CaptureError` — **may follow**.
- T5 `Deviation.to_dict` keys not asserted; `path_from_disk` success path untested; advisor test `recorder=None` — **may follow**.
- T3b deep_routing 16 nodes vs ≤ 12 — done via errata; transform_pipeline one sink vs two — **may follow** (calibration); three cases require a volunteered passthrough — **may follow but WATCH**: this is the single largest floor-legitimacy risk in the corpus (a composer that omits a no-op node is `wrong_shape` for an implementation detail); the README already names it.
- T3b (RECOMMENDED) `test_scenario_is_sound` never passes the scenario's own `option_assertions` — **must fix before merge** (one line; I ran the check by hand — 19/19 pass today — but nothing pins it).
- T6 3 of 5 degraded reasons untested — **may follow**; `_compare` corpus_version refusal unreachable — **not worth doing** (late-binding guard is the real gate); `--force-compare` help text — **may follow**; floors_sha refusal names two digests — **may follow** (name the differing case); unknown case dir ⇒ traceback not 65 — **may follow**; N=5/±44 hard-coded — **may follow** (derive from `ci_half_width_pp(5)`).
- T7 `firing.json.session_id` dead — **may follow** (populate it; `_record` has no access to the sid today — return it from `run_prompt` or drop the field); pagination error clobbers first cause — **may follow**; `_local_skill_hash` bare except — **may follow** (add a source sentinel); `cleanup()` bypasses the step seam — **must fix before merge together with I5** (it is being touched anyway); `_settle` limit=500 no offset — **not worth doing** (probe, not capture); `_verdict` dead `case` param — **not worth doing**; untested branches list — **may follow** (429, preferences mismatch, `MAX_PAGES` exhaustion are the ones worth a test each); `case_flags` no reader — **may follow** (= M6).
- T8 tripwire "not fired" branch untested; wrapper docstring line count — **not worth doing / may follow**.
- conftest `insert(0)` → `append` — **may follow** (M7; trivial, do it in the same fix wave).

Rulings — **[agree | disagree — why]**:

- Execute on `release/0.7.2` in the shared checkout, pathspec commits — **agree** (per-commit path audit clean).
- T2 I1 (fork_coalesce payload oracle-checked one task later in `test_corpus.py`) — **agree** (verified: `test_scenario_is_sound` + anchor test parametrize over every present scenario).
- T2 I2 (nested surface keys loud) — **agree**.
- T3 split 3a/3b, T4 concurrent with 3b — **agree**.
- 3b `variant: "top_k"` — **agree** (matches `_pick_settings_yaml`).
- T5 I1 `wire_blob_inline_ref` = bind half of the detour, never `first_mut`/repair/backtrack — **agree**, with the caveat that it does write CompositionState (M3); the schema-before-mutation criterion is about pipeline authoring, so the convention is defensible.
- T5 I2 `attempted_any`/`first_mut` on non-data mutations only — **agree** (a blob-only thread reads as `passivity`/`decline` + `data_setup_detour`, which is the truthful product finding).
- 3b provisional (threshold_gate empty assertions) → superseded by 3b I2 — **agree** with the supersession.
- 3b I1 zero-node payloads allowed, ≥ 1 output required — **agree** (server accepts source→sink; the anchor test still commits it).
- 3b I2 `condition_literal` membership assertion — **agree**; it is the right shape for v1 (numeric equality, not string-exact). Two things to keep in view: it is *presence*, not attribution (errata (d) says so), and the literal regex reads a quoted `'500'` as numeric (permissive direction — acceptable).
- 3b I3 schema fields authored, no value assertion — **agree**.
- 3b I4 template_lookups ≡ openrouter_sentiment accepted + named blind spot — **agree** (errata (c) + README carry it). Suggest the operator consider whether v1 should keep two case names for one shape or drop one until v2.
- 3b I5 reword deep_routing/error_routing failure-path prompts — **agree**; both re-classify AMBIGUOUS and read as error-routing.
- T6 I1 `mde_pp` → `ci_half_width_pp` — **agree**; the errata note is exactly right.
- T6 I2 delta sign test — **agree**.
- T7 I1 canary verdicts feed the abort streak — **agree** (that is what "the canary proves the instrument alive" means).
- T7 I2 `_identity` never does I/O — **agree** (containment re-entrancy hole; test present).
- T7 I3 exit codes 64/70/1 — **agree**.
- Errata block on the spec rather than body rewrites — **agree**; but the errata should also record the two taxonomy additions the plan made beyond §3 (`abandoned_mutation` hard, `unattributed` severity, `below_floor` flag) — they are in the plan's self-review, not in the spec the operator will read.
- T9 measure the timing figure reproducibly — **agree**.

---

## Recommendations

1. Fix C1, C2, I1–I6 before merge (all are small: the largest is C2's three `http_unrecovered` sites + one precedence tweak + tests). Re-run the full `pytest tests/` after — the conftest change (M7) is the only thing that could touch whole-tree behaviour.
2. **Spec/plan issues to surface to the operator (not implementation defects):**
   - Spec §3/§5: state explicitly that `red_criteria.passivity_phrases` feed the `passivity` class only when no pipeline mutation was applied, and that `red` is a reported flag, not part of `clean` — the plan read it the other way and C1 is the result.
   - Spec §3: `surface: undetermined` from **zero audit rows** is grouped with "planner rows present" under one measurement-flavoured sub-kind; a zero-row non-200 is an instrument fault and should be its own instrument kind (or `terminal_missing`) — see C2.
   - Spec §3: `no_calls` (instrument-error enum) vs `malformed_output` (hard, counted) conflict when every call is malformed (M10).
   - Spec §3 table lacks `abandoned_mutation`; add it (or fold into `repair`) in the errata.
   - The "volunteered passthrough" cases (`explicit_routing`, `schema_contracts_demo`, `canary`) are the floor-legitimacy risk to read first at calibration.
   - `.gitignore` fence in the plan (L824) is the source of I1.
3. Before the calibration firing: run the whole driver once with `--cases canary --repeats 1 --no-tripwire` against the local substrate and read `meta.json` by eye — the fixture layer (`threadgen`/`fake_http`) is faithful to the wire *as read from source*, but nothing in the tree is a captured artefact yet (the `run_ideal` fixture is hand-authored: its `state.json` uses `state_id`, the wire uses `id`; `fake_http` returns 404 for no-state, the wire returns 200+null). Every scorer test rests on the builder; one real capture will confirm the seam cheaply.

---

## Assessment

**Ready to merge?** **With fixes.**

**Reasoning:** The instrument is architecturally sound, spec-faithful in its load-bearing parts (currency, oracle, durable pair, abort rules, immutability, honesty of the report), and the tests are the mutation-resistant kind — but two measurement defects would corrupt the very first firing: a courteous closing sentence flips a clean/optimal run to red with no ledger evidence (C1), and capture-step server failures leak into the denominator or the measurement bucket instead of excluding the run and feeding the abort rule (C2). Both are a few lines each, plus the `.gitignore` mirror (I1) and tripwire containment/immutability (I2); nothing requires a design change or a re-plan.
