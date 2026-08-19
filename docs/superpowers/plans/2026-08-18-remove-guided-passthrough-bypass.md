# Remove the Guided Pass-Through Synthesis Bypass (elspeth-b4a286d517)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the server-side pass-through synthesis short-circuit so every guided
step-2→3 transition routes through the provider planner, restore the test coverage the
bypass steered away, and delete the now-dead `prepare_pipeline_plan` sealing gate.

**Architecture:** `plan_guided_pipeline` (service.py) currently short-circuits the
rootless 1×1 first-pass shape and seals a Python-built pipeline via
`prepare_pipeline_plan` with `provider="server"`, zero provider calls, zero
`llm_call_audit` rows. The branch *returns early*; everything after it is the ordinary
provider path (`plan_pipeline` at `service.py:3895`), so removal is a block deletion
with fall-through — the provider path already handles this shape (the unproducible-
fields test drives it today). With the sole `src/` caller gone, `prepare_pipeline_plan`
is dead and is deleted outright (pre-release: no tech debt). The `output_field_gaps`
context enrichment at `service.py:3676-3696` serves every guided plan and **stays**.

**Tech Stack:** Python 3.12, pytest (integration tests use FastAPI `TestClient` +
monkeypatched `_litellm_acompletion`), SQLAlchemy sessions.db assertions.

**Spec:** filigree issue `elspeth-b4a286d517` (description + comments 7785/7786/7790)
and `BUG-guided-passthrough-validation-deadlock.md` (repo root). The composer
invariants in `AGENTS.md` ("The LLM does the job") and ADR-031 are the governing
rules. Ticket is claimed (`assignee=claude`, status `fixing`).

## Global Constraints

- **Before writing any code, read `docs/agents/recent-code-hints.md`** — whole-tree AST
  gates pin dynamic-attribute sites, masquerade sites (tests included), and wire-shape
  templates; a scoped green run proves nothing about them.
- Work directly on `release/0.7.2` in the shared checkout. Stage by pathspec only
  (`git add <files>`), commit only your own hunks; a sibling can sweep staged files.
- Full `pytest tests/ -n 12` before declaring done — scoped runs miss cross-cutting gates.
- Trust-tier gate: compare the `elspeth-lints check --rules all --root src/elspeth`
  finding corpus before vs after (with the `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE`
  prefix); it exits 1 by design — expect *no new findings*, not zero.
- Wardline gate of record: `wardline scan . --fail-on ERROR --fail-on-inert
  --trust-pack scripts.wardline_pack --allow-custom-packs --local-only` (exit 0 required).
- Composer invariants are absolute: no server-authored pipeline structure, no
  tutorial-only branches. Nothing in this plan may add either.
- No hand-edited `judge_metadata_signature`; never shape code around signature churn.
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Notes field on the ticket says **do not re-run the local sessions.db query** — the
  evidence is settled.

## Facts the implementer needs (verified on `release/0.7.2` @ `78649f625`)

- Gate condition calc: `src/elspeth/web/composer/service.py:3653-3660`
  (`passthrough_sketch_shape`).
- Bypass block: `service.py:3697-3791` — builds `sketch_pipeline`, calls
  `prepare_pipeline_plan(provider="server",
  model_identifier="composer-guided-passthrough-synthesis")`, `return plan, catalog_ids`.
- The comment block at `service.py:3661-3675` explains the gap computation and
  references `passthrough_sketch_shape` — needs rewording, not deletion (the gap
  enrichment at `:3676-3696` survives).
- `prepare_pipeline_plan`: `src/elspeth/web/composer/pipeline_planner.py:2902` (through
  the end of that function), `__all__` entry at `:4567`, import in `service.py:135`.
  Stale comment `:2927` still cites the recipe router excised by `9700470e2`.
- `guided_reviewed_sink_options` in service.py: local import `:3560` block + call
  `:3730` — both die with the bypass (the binder use in `guided/planning.py:2497`
  survives untouched). `deep_thaw` / `canonical_json` / `stable_hash` have other uses
  in service.py — their imports stay.
- Test consumers of the bypass/helper (complete list, from `git grep`):
  1. `tests/integration/web/composer/guided/test_respond.py:891`
     `test_rootless_step_3_entry_synthesizes_the_sketch_without_a_provider_call`
     (parametrized `("live", "tutorial")`) — poisoned provider + server-provenance +
     zero-audit assertions. **Inverted by Task 1.**
  2. `test_respond.py:982`
     `test_rootless_step_3_entry_with_unproducible_output_fields_never_seals_the_sketch`
     — already drives the provider path with a scripted completion; survives with
     docstring rewording only (Task 2).
  3. `test_respond.py:3327` `test_policy_refusal_answers_422_policy_blocked_not_a_provider_fault`
     — stubs `plan_guided_pipeline` directly, unaffected; its docstring cites
     `prepare_pipeline_plan` and `test_server_derived_rejection_carries_its_closed_codes`
     → reword (Task 3).
  4. `tests/unit/web/composer/test_pipeline_planner.py:1510`
     `test_server_derived_rejection_carries_its_closed_codes` — unit test of the
     deleted function (Task 3).
  5. `tests/integration/web/composer/guided/test_proposal_audit_projection.py:1767`
     — `inspect.signature(prepare_pipeline_plan)` authority block + import at `:43` (Task 3).
  6. `tests/integration/web/composer/test_freeform_pipeline_planner.py:1074`
     `test_incomplete_recipe_request_falls_back_before_custody_without_side_effects`
     — monkeypatches `service.prepare_pipeline_plan` as a never-called sentinel (Task 3).
- Steered/deleted coverage from the introducing commit `b073d248e` (4 files):
  - `test_respond.py` provider-outcome matrix in `TestStep2IntraStep`: tutorial row
    `("tutorial", "tutorial_profile")` deleted from the `(profile, expected_surface)`
    parametrize; the rootless entry (`if profile == "tutorial": POST /guided/start
    {"profile": "tutorial"}`) replaced by an unconditional root-intent start with the
    comment "A ROOT INTENT keeps the step-2→3 entry on the provider planner path".
    A second test (~`:802` in the same class) got the same root-intent injection.
  - `tests/integration/web/composer/parity/conftest.py` (~`:641`): rootless walks start
    with a fixture root intent; comment claims a rootless walk "could never derive a
    transform-ful fixture graph from its sole planner call" — false after removal.
  - `tests/unit/web/composer/test_service.py:133`, `:213`, `:405`:
    `root_intent_message_id="33333333-…"` injections keeping unit shapes off the gate.
- Provider-stub idiom in `test_respond.py`: `_PlannerResponse`/`_PlannerChoice`/
  `_PlannerMessage`/`_PlannerToolCall`/`_PlannerFunction` (`:104`), terminal tool call
  `emit_pipeline_proposal` with a stable-id delta pipeline (`source_routes` /
  `output_targets`), monkeypatch `elspeth.web.composer.service._litellm_acompletion`.
  The gap test at `:1000-1200` is the reference implementation, including
  `_full_guided_session(reviewed)` (`:237`) for stable-id retrieval and the
  `ComposerServiceImpl` availability-stub rebuild (`:938-951`).

---

### Task 1: Invert the provenance pin (red)

Rewrite the poisoned-provider test so it pins the **new** contract: the rootless 1×1
step-2→3 entry routes through the provider planner, seals with model provenance, and
emits LLM audit rows. This is the per-transition provenance assertion comment 7785
called for — it must fail on HEAD before Task 2 lands.

**Files:**
- Modify: `tests/integration/web/composer/guided/test_respond.py:891-978`

**Interfaces:**
- Produces: test name `test_rootless_step_3_entry_routes_through_the_provider_planner`
  (both profiles), relied on by Task 4's comment reverts which cite the old name.

- [ ] **Step 1: Read `docs/agents/recent-code-hints.md` in full** (global constraint;
  do it before any edit in this plan).

- [ ] **Step 2: Rewrite the test.** Replace the body of
  `test_rootless_step_3_entry_synthesizes_the_sketch_without_a_provider_call` — keep
  the `("live", "tutorial")` parametrize and the entire choreography up to and
  including the `ComposerServiceImpl` availability-stub rebuild, then replace the
  poisoned completion and assertions:

```python
    @pytest.mark.parametrize("profile", ("live", "tutorial"))
    def test_rootless_step_3_entry_routes_through_the_provider_planner(
        self,
        composer_test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        profile: str,
    ) -> None:
        """The rootless step-2→3 entry is planned by the provider, like every transition.

        elspeth-b4a286d517: this transition was server-synthesized
        (provider="server", zero provider calls, zero llm_call_audit rows) —
        banned by the composer invariant (the LLM does the job) and by ADR-031
        (the tutorial exercises the same backend as every guided walk). The
        pass-through answer itself is fine; its AUTHOR must be the planner.
        """
        # ... unchanged choreography from the current test through the
        # ComposerServiceImpl rebuild (availability provider="test",
        # model="test/guided-planner") ...
        reviewed = _respond(composer_test_client, session_id, chosen=["text"], custom_inputs=[])
        guided_facts = _full_guided_session(reviewed)
        source_stable_id = next(iter(guided_facts["reviewed_sources"]))
        output_stable_id = next(iter(guided_facts["reviewed_outputs"]))
        reviewed_output_name = guided_facts["reviewed_outputs"][output_stable_id]["name"]
        planner_pipeline = {
            "source_routes": [{"stable_id": source_stable_id, "on_success": reviewed_output_name}],
            "nodes": [],
            "edges": [],
            "output_targets": [{"stable_id": output_stable_id}],
        }

        provider_calls: list[Mapping[str, Any]] = []

        async def terminal_completion(**kwargs: Any) -> _PlannerResponse:
            provider_calls.append(kwargs)
            return _PlannerResponse(
                choices=[
                    _PlannerChoice(
                        message=_PlannerMessage(
                            content=None,
                            tool_calls=[
                                _PlannerToolCall(
                                    id="guided-terminal",
                                    function=_PlannerFunction(
                                        name="emit_pipeline_proposal",
                                        arguments=json.dumps({"pipeline": planner_pipeline}),
                                    ),
                                )
                            ],
                        )
                    )
                ],
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
            )

        monkeypatch.setattr("elspeth.web.composer.service._litellm_acompletion", terminal_completion)

        settled = _post_current_response(
            composer_test_client,
            session_id,
            component_action={"action": "finish", "component_kind": "output"},
        )

        assert settled.status_code == 200, settled.json()
        assert settled.json()["next_turn"]["type"] == "propose_pipeline"
        assert provider_calls, "the step-2→3 entry must be planned by the provider"
        with app.state.session_engine.connect() as conn:
            proposal = conn.execute(
                select(
                    composition_proposals_table.c.composer_model_identifier,
                    composition_proposals_table.c.composer_provider,
                ).where(composition_proposals_table.c.session_id == session_id)
            ).one()
        assert proposal.composer_provider != "server"
        assert proposal.composer_model_identifier != "composer-guided-passthrough-synthesis"
        audit_messages = asyncio.run(app.state.session_service.get_messages(UUID(session_id), limit=None))
        llm_audits = [
            envelope for message in audit_messages for envelope in (message.tool_calls or ()) if envelope.get("_kind") == "llm_call_audit"
        ]
        assert llm_audits, "a planned transition must leave llm_call_audit evidence"
```

  Where the choreography differs per profile, keep the current test's
  `if profile == "tutorial": POST /guided/start {"profile": "tutorial"}` entry —
  rootless is the point; do NOT add a root intent. After first green run, tighten the
  two `!=` provenance assertions to `==` against the values actually recorded for the
  stubbed availability (`provider="test"`, model `test/guided-planner`) — pin truth,
  not just non-server.

- [ ] **Step 3: Run to verify it fails on HEAD.**
  `source .venv/bin/activate && pytest "tests/integration/web/composer/guided/test_respond.py::TestStep2IntraStep::test_rootless_step_3_entry_routes_through_the_provider_planner" -x`
  Expected: FAIL — `provider_calls` empty / provenance is `server` (the bypass still
  short-circuits). If it PASSES here, stop: the premise is wrong; re-verify the gate.

- [ ] **Step 4: Commit** (test-first red is committable in this repo's flow only with
  the fix — hold the commit; Task 2 commits both together, per repo convention of not
  landing a red tree on the shared checkout).

### Task 2: Remove the bypass (green)

**Files:**
- Modify: `src/elspeth/web/composer/service.py:135, 3560, 3653-3791`
- Modify: `tests/integration/web/composer/guided/test_respond.py:982-1030` (docstring only)

**Interfaces:**
- Consumes: Task 1's rewritten test as the acceptance gate.
- Produces: `plan_guided_pipeline` with a single planning path; `prepare_pipeline_plan`
  now has zero `src/` callers (Task 3 deletes it).

- [ ] **Step 1: Delete the gate calc** `service.py:3653-3660` (`passthrough_sketch_shape = (...)`).

- [ ] **Step 2: Reword the comment block `:3661-3675`.** It currently explains that the
  gap is computed "for EVERY guided plan, not just the sketch" and references
  `passthrough_sketch_shape`. Replace with the surviving truth, e.g.:

```python
        # A zero-transform pipeline emits exactly what the reviewed source
        # carries, so a declared sink field no source can supply makes it
        # unbuildable. Validation cannot be the guard (R2-F4): the contract
        # check fires only when the producer participates in propagation
        # (an observed-schema source abstains under ADR-007), and even then as
        # an opaque sink_contract_violation the planner cannot repair away.
        # The gap is therefore named to the planner up front, and the planner
        # loop refuses any zero-transform candidate carrying it
        # (passthrough_cannot_produce_declared_fields). That is not a general
        # satisfiability gate — with a transform present a field may
        # legitimately be produced, and the loop's guard says nothing.
```

- [ ] **Step 3: Delete the bypass block `:3697-3791`** (`if passthrough_sketch_shape and
  not output_field_gaps:` through `return plan, catalog_ids`). The gap-context
  enrichment `if output_field_gaps:` block stays. Control now falls through to the
  provider path unconditionally.

- [ ] **Step 4: Strip stranded imports.** Remove `prepare_pipeline_plan` from the
  `service.py:135` import and `guided_reviewed_sink_options` from the local import
  block at `:3560`. Leave `deep_thaw`/`canonical_json`/`stable_hash` (still used).
  Note the ruff pre-commit hook strips unused imports itself — do it explicitly anyway
  so the hook doesn't rewrite mid-commit.

- [ ] **Step 5: Update the gap test's docstring** (`test_rootless_step_3_entry_with_
  unproducible_output_fields_never_seals_the_sketch`, `:982`): the narrative "it must
  skip the sketch and route to the provider planner" becomes "the provider planner is
  the only path; what is pinned is that the gap is named in the reviewed planner
  context and no zero-transform candidate seals while it stands". Rename to
  `test_step_3_entry_with_unproducible_output_fields_names_the_gap_to_the_planner` if
  the old name reads as sketch-dependent (keep assertions byte-identical — including
  `composer_provider != "server"` over the whole corpus, which is now a cheap standing
  provenance sweep).

- [ ] **Step 6: Run the affected file.**
  `pytest tests/integration/web/composer/guided/test_respond.py -x -q`
  Expected: Task 1's test PASSES; the gap test passes both parameters; the two
  root-intent-steered matrix tests still pass (they run the provider path regardless).

- [ ] **Step 7: Commit** (service.py + test_respond.py by pathspec):

```bash
git add src/elspeth/web/composer/service.py tests/integration/web/composer/guided/test_respond.py
git commit -m "fix(composer): the rootless step-2→3 entry is planned by the provider, not synthesized (elspeth-b4a286d517)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

### Task 3: Delete `prepare_pipeline_plan` and rewire its consumers

**Files:**
- Modify: `src/elspeth/web/composer/pipeline_planner.py` (delete `:2902`-end-of-function; `__all__` `:4567`)
- Modify: `tests/unit/web/composer/test_pipeline_planner.py` (delete the server-derived test + its private helpers if now unused, drop the `:78` import)
- Modify: `tests/integration/web/composer/guided/test_proposal_audit_projection.py:43,1767`
- Modify: `tests/integration/web/composer/test_freeform_pipeline_planner.py:1060-1090`
- Modify: `tests/integration/web/composer/guided/test_respond.py:3344` (docstring)

**Interfaces:**
- Consumes: Task 2 (zero `src/` callers).
- Produces: no symbol named `prepare_pipeline_plan` anywhere in the tree; no
  `provider="server"` sealing entry point exists at all.

- [ ] **Step 1: Verify zero src callers before deleting:**
  `git grep -n "prepare_pipeline_plan" src/` → expected: definition + `__all__` only.

- [ ] **Step 2: Delete the function** at `pipeline_planner.py:2902` (docstring "Prepare a
  server-derived pipeline through the planner's final gate") through the end of that
  function body, and its `__all__` entry at `:4567`. The stale R2-F10 "recipe router,
  guided sketch" comment at `:2927` dies with it. Check whether the deletion strands
  private helpers used only by it (e.g. the `_build_valid_pipeline_plan` single-shot
  conversion is shared with the model loop — it stays; verify with `git grep`).

- [ ] **Step 3: Unit tests.** Delete
  `test_server_derived_rejection_carries_its_closed_codes` (test_pipeline_planner.py
  `:1510`) and the `prepare_pipeline_plan` import (`:78`). Its semantics — closed
  rejection codes carried to the surface — remain pinned on the surviving path by the
  gap test's `rejection_codes` assertion (`test_respond.py:1185-1195`) and the
  `_rejection_exhausted` coverage; verify with
  `git grep -ln "detail_codes\|rejection_codes" tests/ | head` that model-path carriage
  is still asserted somewhere, and name that test in the commit message.

- [ ] **Step 4: Signature-authority test.** In `test_proposal_audit_projection.py:1767`
  remove the `server_signature` block (three-parameter default check +
  `covered_deferred_intent_ids` absence check) and drop `prepare_pipeline_plan` from
  the `:43` import. The `plan_pipeline` and `verified_remaining_deferred_intents`
  blocks stay.

- [ ] **Step 5: Freeform fallback test.** `test_incomplete_recipe_request_falls_back_
  before_custody_without_side_effects` (`test_freeform_pipeline_planner.py:1063`)
  monkeypatches `service.prepare_pipeline_plan` as a never-called sentinel — the
  attribute no longer exists, so `monkeypatch.setattr` raises `AttributeError`. Read
  the test in full. The recipe router it guarded was excised by `9700470e2`; if the
  compose loop no longer has any recipe-shaped branch, the never-reaches-custody
  sentinel is guarding a path that cannot exist and the test reduces to "freeform
  routes to `plan_pipeline`" — keep that half (the `fallback` AsyncMock and its
  assertion) and delete the sentinel patch. If a recipe branch *does* still exist in
  the compose loop, stop and re-scope: that is a live server-derived path this plan
  must account for, not silently patch over. Record the outcome in the commit message.

- [ ] **Step 6: Reword the route-classification docstring** at `test_respond.py:3344`:
  "The planner exception raised here is exactly what `prepare_pipeline_plan` produces
  on that path" → "The planner exception raised here matches what the guided planner
  surfaces on a policy refusal (`VALIDATION_FAILED` carrying the gate's closed code)".
  Drop the reference to the deleted unit test.

- [ ] **Step 7: Verify absence:**
  `git grep -n "prepare_pipeline_plan\|passthrough-synthesis\|passthrough_sketch_shape" -- src tests`
  → expected: zero hits (`passthrough_cannot_produce_declared_fields` is a different,
  surviving symbol — don't over-match).

- [ ] **Step 8: Run the three touched test files, then commit** by pathspec:
  `pytest tests/unit/web/composer/test_pipeline_planner.py tests/integration/web/composer/guided/test_proposal_audit_projection.py tests/integration/web/composer/test_freeform_pipeline_planner.py -q`

```bash
git commit -m "refactor(composer): delete prepare_pipeline_plan — no server-derived sealing gate exists (elspeth-b4a286d517)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

### Task 4: Restore the steered and deleted coverage

The introducing commit didn't delete most of its coverage — it steered it onto rooted
shapes so the bypass shape was never asserted. Reverse each steering.

**Files:**
- Modify: `tests/integration/web/composer/guided/test_respond.py` (provider-outcome matrix + second injected start, ~`:668` and `:802` regions)
- Modify: `tests/unit/web/composer/test_service.py:133,213,405`
- Modify: `tests/integration/web/composer/parity/conftest.py:641-655`

**Interfaces:**
- Consumes: Task 2 (rootless shapes now route through the provider planner).

- [ ] **Step 1: Provider-outcome matrix.** Restore the tutorial row:
  `(("live", "guided_staged"), ("tutorial", "tutorial_profile"))`, and restore the
  original rootless entry choreography that `b073d248e` replaced:

```python
        if profile == "tutorial":
            started = composer_test_client.post(
                f"/api/sessions/{session_id}/guided/start",
                json={"profile": "tutorial", "operation_id": str(uuid4())},
            )
            assert started.status_code == 200, started.json()
```

  Delete the unconditional root-intent start and its "A ROOT INTENT keeps the
  step-2→3 entry on the provider planner path" comment. Apply the same revert to the
  second injected start (~`:802` region, comment "Root intent keeps the step-2→3 entry
  on the provider planner path"). Use `git show b073d248e -- tests/integration/web/composer/guided/test_respond.py`
  as the authoritative before-image.

- [ ] **Step 2: Unit test steering.** For each of `test_service.py:133`, `:213`, `:405`:
  read the surrounding test; where `root_intent_message_id="33333333-…"` was added by
  `b073d248e` purely to stay on the provider path (its comment says so), remove it so
  the unit shape is rootless again. Where a test legitimately exercises rooted
  behaviour (e.g. `:405` — verify against `git show b073d248e -- tests/unit/web/composer/test_service.py`,
  which only added 6 lines), leave it. Comments claiming the root intent is
  load-bearing for provider routing must not survive.

- [ ] **Step 3: Parity conftest.** The else-branch comment (`:641`) claims a rootless
  walk "could never derive a transform-ful fixture graph from its sole planner call".
  That premise is now false — but the fixture's scripted completion emits exactly one
  proposal response, and a rootless walk now consumes a planner call for the
  pass-through entry before the transforms turn. Keep the root-intent start as
  *fixture choreography* and rewrite the comment to say so honestly:

```python
            else:
                # Fixture choreography, not a routing necessity: the scripted
                # completion answers exactly one planner call, so the walk
                # starts with a root intent to spend it on the transform-ful
                # fixture graph. (The rootless step-3 entry is provider-planned
                # too — elspeth-b4a286d517 — it would just consume a scripted
                # response this fixture does not carry.)
```

  Do not expand the parity fixture to double-scripted rootless walks in this plan —
  that is new coverage, not restoration; note it on the ticket if it looks warranted.

- [ ] **Step 4: Run** `pytest tests/integration/web/composer/guided/test_respond.py tests/unit/web/composer/test_service.py tests/integration/web/composer/parity -q`
  Expected: all pass; the restored tutorial matrix row runs the provider-outcome
  matrix through the tutorial profile again (ADR-031 canary restored).

- [ ] **Step 5: Commit** by pathspec:

```bash
git commit -m "test(composer): restore the rootless + tutorial coverage b073d248e steered away (elspeth-b4a286d517)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

### Task 5 (decision point — recommended, severable): per-read provenance guard

Comment 7785: the durable assertion is per-transition — "no guided proposal may be
staged with `composer_provider='server'`". After Tasks 2-3 no code path can *produce*
one, so the honest place for a runtime guard is the existing provenance-completeness
seam that already inspects `composer_provider` on read:
`src/elspeth/web/sessions/routes/composer/proposals.py:87`
(`_missing_proposal_composer_context`).

- [ ] **Step 1: Enumerate writers first.**
  `git grep -n "composer_provider" src/ | grep -v "routes/composer/proposals.py"` and
  confirm nothing legitimate records `"server"` (check the recipe MCP tools
  `apply_pipeline_recipe` — if they seal proposals under a distinct provider value,
  scope the guard so it does not fire on them). If any legitimate `server` writer
  exists, STOP and surface to John — the guard as specified would break it.

- [ ] **Step 2: Write the failing test** (unit-level, next to the existing
  `_missing_proposal_composer_context` coverage — locate with
  `git grep -ln "_missing_proposal_composer_context" tests/`): insert a proposal row
  with `composer_provider="server"` and assert the completeness check reports it as
  invalid provenance (exact assertion shape follows the neighbouring tests for that
  helper).

- [ ] **Step 3: Implement** — extend `_missing_proposal_composer_context` to treat
  `composer_provider == "server"` as missing/invalid provenance.

- [ ] **Step 4: Mutation-check the guard** (memory doctrine: mutation-test the guard,
  not the defect): temporarily revert the Step 3 hunk, confirm the Step 2 test fails,
  restore. Then run the file, commit both by pathspec:

```bash
git commit -m "feat(composer): provider='server' is never valid proposal provenance (elspeth-b4a286d517)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

If John prefers not to carry a guard whose producer no longer exists, drop this task
and record the decision on the ticket — Tasks 1-4 already pin the contract in tests.

### Task 6: Whole-tree verification and reconciliation

- [ ] **Step 1: Full suite.** `pytest tests/ -n 12` — the plain default selection is the
  CI-equivalent run. Capture HEAD before AND after the run (shared checkout; a
  sibling's mid-run commits can produce phantom failures — re-run any failure against
  a stable HEAD before believing it).

- [ ] **Step 2: Gates.** Run the wardline gate of record and the trust-tier lint
  compare (commands in Global Constraints). Expected: wardline exit 0; lint corpus
  unchanged relative to the pre-change baseline (export baseline via full
  `git archive HEAD` tree if comparing, per memory doctrine).

- [ ] **Step 3: Stale-reference sweep.**
  `git grep -n "recipe router\|passthrough.synthesis\|server-synthesiz\|server-derived" src/ tests/ docs/`
  and fix any comment/doc still describing the bypass as current behaviour (known:
  none in `docs/` per pre-plan grep, but the sweep is cheap). ADR-031 needs no edit —
  removal *restores* its stated property.

- [ ] **Step 4: Reconcile `BUG-guided-passthrough-validation-deadlock.md`.** Its root
  cause (fixed-point, no-repair server path) is deleted by this change; its suggested
  fixes 1/2/4 are mooted (the provider loop has repair + receives the named gap). Its
  §"Diagnosability defect" — the validator's message is unrecoverable from logs
  (`_log_guided_planner_failure` truncates to the generic message) — SURVIVES and is
  not covered by this plan: file it as its own filigree bug (component
  `web/composer (guided planning path)`, cite the BUG file's §Diagnosability and
  incident session `847ef691`), then ask John whether to delete the now-reconciled
  BUG file from the repo root (it is untracked and operator-created — do not delete
  unprompted).

- [ ] **Step 5: Ticket close-out.** Comment on `elspeth-b4a286d517` (CLI form —
  MCP writes fail against a claimed issue:
  `filigree --actor claude add-comment elspeth-b4a286d517 "TEXT"`): what landed, the
  commit SHAs, the coverage restored, the Task 5 decision, the follow-up bug ID from
  Step 4. Set `fix_verification` and transition `fixing → verifying`
  (`filigree --actor claude update … -f 'fix_verification=…'`); bugs cannot
  direct-close, and merged-but-unverified stays in `verifying` for the operator.
  Note for `elspeth-63cf3803e6` on ITS ticket: the rootless 1×1 turn-count is now
  measurable live; the owed measurement fires against
  `--base unix:///run/elspeth/uvicorn.sock` (never the hostname/edge).

- [ ] **Step 6: Do NOT push.** The branch already carries unpushed sibling work; pushing
  is John's call at package completion.

## Explicitly out of scope (recorded so it isn't silent deferral)

- **Frontend harness per-transition call accounting** (`tutorial-harness.ts:514` counts
  per-walk): the staging-harness gate (`redundantSelections` / phase-shape violations,
  live since `752e404ea`) is the current backstop, and the un-raised
  `REDUNDANT_STATE_LIST_TOOLS` assertion gap belongs to `elspeth-63cf3803e6`'s surface.
  If John wants the per-transition zero-call check in the harness too, it is a separate
  frontend change.
- **Live latency/turn-count measurement** of the restored transition: owed to
  `elspeth-63cf3803e6` (its brief fixes landed in `e69e46070`), measurable only after
  this plan lands, fired at the unix socket.
- **The `blob:<uuid>` tutorial leak** and other open composer items — untouched.
