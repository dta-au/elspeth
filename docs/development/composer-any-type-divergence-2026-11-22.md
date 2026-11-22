# Producer `any` into a typed consumer: composer and runtime disagree

Date: 2026-11-22. Code base read: integration/0817 at 06c9cd408 plus the fan-in walk fix
(commit 2b5a5326a). Status: analysis and recommendation only. Nothing in `src/` or `tests/` was changed.

Evidence labels: **[measured]** means a probe or command run for this note, with its output kept;
**[read]** means read from source, docs or git; **[inferred]** means reasoned from the other two
and not run.

## Summary and recommendation

**What the question was.** If a producer declares `price: any` and a consumer declares `price: str`,
the runtime build refuses the pipeline ("expected str, got Any"), but the composer reports it valid.

**What the probes found [measured].** The divergence is not one shape. The probes found **eight**
shapes where the runtime refuses and the composer says valid: a source, a transform, a forwarding
pass-through, a gate, a union coalesce and an untyped value_transform target. Each of these
producers declares `any` into a node, a sink or an optional consumer. On the shapes that reach the
field only through the guarantee walk, both surfaces abstain and agree: an under-declaring
forwarder, an observed forwarder, and a json_explode element.

**Which side is right [read].** For the direct edge, the runtime is right. ADR-050 Decision 10 says
in so many words that "a typed consumer of an untyped target is refused at build time". The
runtime's own repair advice was written for exactly this refusal (elspeth-8762d9b666, live session
891b7b1e). The abstention in `_edge_field_type_conflict` gives three reasons:

- "no conflict is mechanically provable";
- "a false red misdirects the authoring loop";
- "a missed one is caught downstream".

The first reason fails for the direct arm: the runtime's verdict is a pure function of the two
declarations. The third still holds, but only at a cost (see "Planner experience").

**Where the drift comes from [measured + read].** The runtime treats `any` in two ways:

- the declared arm (`check_compatibility`) treats `any` as a top type that promises nothing;
- the guarantee-walk arms (`resolve_guaranteed_field_type`, mode `edge`) treat `any` as "no type
  stated" and abstain.

The composer folded both arms into one walk and applied the guarantee-walk rule everywhere. The one
shared type predicate, `declared_type_name_admits`, has a gap exactly where the drift is: it
requires every caller to "abstain on `any` before asking".

**Recommendation.** Option **A, built the C way**. The composer refuses what the runtime's declared
arm refuses. The `any` rule is decided once, in `contracts/`, by making `declared_type_name_admits`
cover `any` as well. The composer tells the declared arm from the guarantee arm by asking whether
the field is in the runtime model of the effective producer. It reads that membership from its
probe instance, not from `_output_schema_config`, because the probe found a trap there (case c6).
The runtime's internal disagreement (c4 against c4-control, a monotonicity break) goes to the
maintainer as an open question. Size: **medium** (inferred: about 150–250 source lines and about
300 test lines, mostly an agreement matrix).

**The maintainer's ruling.** "If it's corrupted it's a bug and the system shouldn't proceed in a
buggy state." A validator that answers "valid" for a graph its own authority refuses is reporting a
known-false verdict, and the composer proceeds on that verdict. That is the defect. Option B, which
relaxes the runtime, runs the other way: the build would accept a contract gap, and the rows it lets
through would fail one at a time as `PluginContractViolation`. The engine labels that failure "an
upstream transform/source schema bug" (`engine/executors/transform.py:564-570`) [read]. So option B
both proceeds in the buggy state and blames the wrong component in the audit trail.

## The measured divergence

Probe: two throwaway probe tests, not committed. They use the same harness as
`tests/integration/pipeline/test_composer_runtime_agreement_fan_in_walk.py`: one YAML goes to the
runtime build (`ExecutionGraph.from_plugin_instances`) and to the composer
(`composition_state_from_runtime_yaml(...).validate()`).

Provenance [measured]: `elspeth.__file__` resolved into the tree under test for the runs
reported here. The first two runs had imported a different checkout: pytest's rootdir had
resolved to the repo root, whose `pythonpath = ["src"]` ini took effect. Those runs were
discarded and rerun with `--rootdir`/`-c` pinned to the tree under test. The verdicts were
identical.

| Case | Shape (consumer declares `str` unless noted) | Runtime | Composer |
|---|---|---|---|
| c1 | fixed source `price: any` → node | RED `expected str, got Any` | valid |
| c1-opt | same, consumer `price: str?` | RED `expected str \| None, got Any` | valid |
| c2 | fixed source `price: any` → sink `fixed` | RED | valid |
| c3 | source `str` → passthrough declaring `price: any` → node | RED | valid |
| c4-control | source `any` → forwarder declaring `price: any` → node | RED | valid |
| c8 | value_transform target from an `any` input, untyped → node | RED `expected str, got typing.Any \| None` | valid |
| g1 | source `any` → routing gate → node | RED | valid |
| u1 | fork → two branches declaring `price: any` → union coalesce → node | RED (`_CoalesceUnionSchema_1`) | valid |
| c4 | source `any` → forwarder declaring only `id` → node | green | valid |
| c5 | source `any` → observed forwarder → node | green | valid |
| c6 | field_mapper rename of an undeclared source field → node | green | valid |
| c7 | json_explode element (`any` stamp) → node / sink | green | valid |
| r1 (repair) | source `any` → type_coerce `price→str` → node | green | valid |
| controls | source `str` → node; source `int` → node; source `str` → consumer `any` | green / RED / green | valid / `edge_field_type_incompatible` / valid |

The `int` control shows the composer's type check firing, and both surfaces agree on it. That
rules out the explanation that the composer check is simply not running.

An adjacent case, recorded but not in scope [measured]: r2. A value_transform that types its target
`label: str?` meets a consumer that declares `label: str`. The runtime refuses it (`str | None` vs
`str`); the composer says valid. This is nullability, not `any`. The composer compares base types
only, and does so on purpose (`web/composer/state.py:7340-7357`).

Agreement inventory [measured, `pytest --collect-only` on the three agreement files]: 302 tests.
Six of them involve `any`, all at a union coalesce or an LLM authored-output declaration. None
covers the declared edge arm.

## Semantics inventory: where `any` is interpreted

| Site | Side | Meaning | Evidence |
|---|---|---|---|
| `contracts/schema.py:41,51-56` `FIELD_TYPE_MAP` | DSL → pydantic | `any` → `typing.Any` | read |
| `contracts/schema_contract_factory.py:20-26` | DSL → contract | `any` → `object` ("accepts anything") | read |
| `contracts/schema_contract.py:321-355` `SchemaContract.validate` | consumer, per row | top type: skip the type check, presence still checked | read |
| `engine/executors/transform.py:564` input `model_validate(strict=True)` | consumer, per row | a value is checked against the consumer's type; the producer's `any` is never consulted | read |
| `contracts/data.py:375-424` `_types_compatible` (declared arm of `check_compatibility`) | consumer `Any` | admits everything (`:407`) | read |
| same | producer `Any` | **top type: refuses every concrete consumer** | measured c1–u1 |
| `contracts/data.py:427-458` `resolved_guarantee_type_mismatch` | producer, guarantee walk | **abstain**: "would manufacture rejections for a type nobody stated" | read; measured c4, c5 |
| `core/dag/guarantees.py:548-552` `ResolutionMode` | producer | `edge`: abstain; `union_merge`: `any` is a known type | read |
| `contracts/union_merge.py:222` `certain_union_type_conflict` (shared by build and composer) | union branches | `any` is a type of its own, conflicts with `int` (ADR-050 D8) | read; existing agreement tests |
| `core/dag/schema_validation.py:1135-1175` `row_union_schema_configs_compatible` | row_union branches | wildcard: skipped | read |
| `core/dag/schema_validation.py:1984-2066` string-typed input fields | producer | abstains on `str`/`any` | read |
| `contracts/schema.py:887-920` `declare_missing_guaranteed_fields` | synthesized placeholder | "type unknown", required, nullable; ADR-050 D1: "not an operator declaration and yields to the plugin's type" | read |
| `contracts/schema_contract.py:57-65` `declared_type_name_admits` (shared) | both | **undefined**: raises `KeyError`; the caller "must abstain before asking" | read |
| `plugins/transforms/llm/base.py:782-797` | authored over written | consumer `any` admits; a concrete declaration over a written `any` (`_usage`) is refused | read |
| `plugins/transforms/llm/__init__.py:63-67` | LLM output | response `str`, `_model` `str`, `_usage` `any` | read |
| ADR-050 D12 | plugin output | `any` "reserved for what the plugin cannot know"; LLM structured outputs are bound to concrete types | read |
| `web/composer/state.py:7290-7319` `_consumer_typed_declarations` | consumer | top type: excluded from the check (agrees with the runtime) | read |
| `web/composer/state.py:7152, 7205, 7221` `_resolved_producer_field_type`, `edge` | producer | **abstain**, on the direct declaration and on ancestors alike | read; measured |
| `web/composer/state.py` deferred-contract notice (`contract_probe_deferred`, `:8637`) | bytes-dependent fields | "unknown": a separate concept from `any` | read |
| `web/composer/skills/pipeline_capabilities.md:355-361` | planner guidance | "use `any` for created fields with varying types"; "provable contradictions may be refused at build" | read |
| `web/composer/skills/pipeline_capabilities.md:486-492` | planner guidance | at a union coalesce, `any` "is a type of its own, not a wildcard" | read |
| `web/execution/_validation_diagnostics.py:533-560` | repair advice | producer `Any` vs a concrete consumer: insert type_coerce; widening to `any` "erases the type contract" | read |
| `web/composer/planner_authoring_aids.py:844-853` | planner aid | narrow an `any` element with type_coerce, "never by widening every downstream consumer" | read |

**Reading.** On the consumer side, `any` is a top type everywhere, with no disagreement. On the
producer side, two meanings coexist:

- **Top type ("promises nothing").** This meaning is held by the declared arm, the union coalesce,
  the LLM config, ADR-050 D8 and D10, and the repair advice.
- **"No type stated", so abstain.** This meaning is held by the guarantee-walk arms, row_union, the
  string-typed scan and the composer.

The abstain meaning has one sound justification: synthesized placeholders. Nobody wrote those, and
ADR-050 D1 says they yield to the plugin's type. It is weaker for an `any` an operator wrote on
purpose. ELSPETH already has a separate, honest representation for "unknown" (observed schemas, and
the deferred-contract notice), so `any` does not need to carry that meaning as well.

**The runtime contradicts itself [measured].** In c4 the forwarder does not declare `price`, so the
guarantee walk finds the source's `any` and abstains: green. In c4-control the forwarder truthfully
declares `price: any`, and the declared arm refuses: red. Declaring more flips green to red. The
forgiven-field pass's own docstring (`core/dag/schema_validation.py:436-441`) says "declaring MORE
can never flip a green build red or a red build green". For `any`, that invariant does not hold.

## Systems analysis

Boundary: the composer's Stage-1 validator (`_check_schema_contracts`), the runtime DAG build, the
composer's turn-end runtime preflight, the planner, and the people who maintain them. Time horizon:
releases since 2026-08.

**Stocks.** Three things accumulate: the mirror's size (`_check_schema_contracts` grew from 2,563 to
3,540 lines between 2026-08-24 and now, per `docs/development/crap-hotspot-risk-assessment-2026-11-22.md`
URGENT 1 [read]); the number of documented abstentions (each one an approved divergence); and the
agreement corpus (302 tests [measured]).

**Loop R1: the mirror grows (reinforcing).**

1. A runtime rule is added or split. For example, the 7d68b04878 and 85e8afa2f5 passes added a
   guarantee arm next to the declared arm, with its own `any` rule.
2. Then a delay: the composer copy lags until a live session, a test run or a review exposes it.
3. Someone patches that one case inside the closure nest and pins it with an agreement test.
4. The function grows, and it gets harder to tell which runtime arm a given mirror site copies.
5. The next patch copies the nearest arm's rule into a place the other arm governs. This case is
   that step: the guarantee walk's `any` abstention was applied to the declared arm.

Evidence [read]: 178 commits touched `web/composer/state.py` since 2026-08-01. At least 16 have a
parity, mirror or runtime subject: a regex over commit subjects, so a lower bound, checked against
the known positive e4b7d5d29.

**Loop B1: detection (balancing).** A divergence goes red only where someone wrote a case for it.
The harness compares verdicts, but its cases are hand-picked. Nobody wrote the declared-arm `any`
case until it turned up by chance during unrelated work, so the loop's gain is capped by what case authors
think of.

**Shifting the burden (archetype; a hypothesis that fits the evidence).**

- The symptomatic fix is the runtime preflight as backstop, plus the doctrine "under-rejecting is
  the correct direction" (`state.py:7362`). Every uncertainty in the mirror resolves to "abstain",
  and abstentions make no test fail.
- The fundamental fix is one rule implementation.
- The backstop works well enough that abstaining looks safe. That removes the pressure to mirror
  precisely, and the mirror's own `any` rule stays undecided.
- Side effect: a backstop refusal costs the planner a repair turn. `_MAX_REPAIR_TURNS = 2`
  (`web/composer/composition_completion.py:174`) [read].

**Loop R2: the planner widens to `any` (reinforcing; observed once).**

1. The `any`/`str` refusal reaches the planner late and far from the edit that caused it.
2. The planner widens the consumer and the sink to `any`. This happened in live session 891b7b1e,
   per the comments at `_validation_diagnostics.py:533-539` and `planner_authoring_aids.py:844-846`
   [read].
3. The graph now holds more `any`, so the composer abstains more often.
4. More refusals arrive late, or rows fail per row.

The runtime advice was changed to break this loop by naming type_coerce. The composer's own
`edge_field_type_incompatible` guidance (`web/composer/tools/generation.py:1608-1611`) does not yet
name the `any` case.

**Alternative explanation, tested.** "The composer check simply does not run on these edges." The
`int` control rules it out: on the same edge, the composer reported `edge_field_type_incompatible`.
The cause is the `any` rule and how the arms are chosen.

**Leverage points.**

| Level | Intervention | Evidence that it works |
|---|---|---|
| Parameter (weak) | Per-case patch: add an `any` branch at `state.py:7152/7205/7221` | It closes these eight shapes but leaves the rule undecided, so R1 continues |
| Information flow | The composer reads which fields are in the producer's runtime model from its probe instance, instead of reconstructing it | `probe_cache.transform(...)` already builds the real plugin; c6 shows reading `_output_schema_config` instead would create a false red |
| Rules (strong) | One `any` rule in `contracts/`, called by both surfaces | Precedents: `declared_type_admits` (int→float, shared) and `certain_union_type_conflict` (shared by `core/dag/builder.py:245` and `state.py:7436`). The existing union-coalesce `any` agreement tests pass on both surfaces through that single predicate |
| Feedback gain | A generated agreement matrix: type pairs × topologies | Raises B1's gain from hand-picked to systematic. Cost estimate [inferred from 19 cases in 1.5 s, measured]: about 300 cases in under a minute |

**Falsification.**

- Suppose that, after the shared rule lands, a mutation of the `any` rule in `contracts/` does not
  turn both surfaces red together. Then the claim "one implementation" is false.
- Suppose the generated matrix still finds `any` disagreements once the type-pair rule is shared.
  Then the residual cause is arm selection (topology), not type semantics. c4 against c4-control
  and c6 already show that arm selection is a real second factor, which is why the recommendation
  covers both.

## Options

| | Correctness | Planner experience | Compatibility | Audit | Cost |
|---|---|---|---|---|---|
| **A** The composer mirrors the runtime refusal | Closes the eight measured shapes. As a hand patch it keeps the `any` rule decided in more than one place | The refusal arrives at the tool call, with an error code (`edge_field_type_incompatible`), and no repair turn is spent. Needs one guidance line naming type_coerce | YAML: none; the runtime is unchanged. Composer graphs: only graphs the runtime already refused now show errors (inferred from the table: every flipped case is already runtime-red). Allowed for 0.8.17 in any case | None at runtime. Validation stops making a false claim | Small as a patch; recurs (R1) |
| **B** The runtime relaxes producer `any` to per-row enforcement | Contradicts ADR-050 D10 and D8 wording, the LLM config rule and the 8762d9b666 repair design. Splits `any` further, because `union_merge` keeps it as a type | The planner sees green, then rows fail during the run. Invites R2 | Accepts YAML that is refused today. Nothing that runs breaks. An ADR-050 amendment is needed | Rows that would have been prevented fail as `PluginContractViolation`, labelled as an upstream plugin bug, routed to `on_error`; with `discard` they are audited row loss. Misattributes an authoring gap | Small in code, high in doctrine |
| **C** One shared rule implementation | Decides `any` once. Needs arm selection as well (c4/c6) | Same as A | Same as A, if the runtime verdicts are left unchanged | Same as A | Medium |
| **D** Leave and document | A known false "valid" stays. Conflicts with the maintainer's ruling | Each occurrence costs one of two repair turns, and the error surfaces late | None | None directly | Zero now; recurring planner cost |

## Recommendation, acceptance and size

**Do A, built through C.** It has four parts.

1. **Contracts (small).** Make `declared_type_name_admits` (`contracts/schema_contract.py:57`)
   cover `any`: a consumer `any` admits every type; a producer `any` admits only a consumer `any`.
   Delete "must be abstained on before asking". The existing callers
   (`plugins/transforms/llm/base.py:797`, `plugins/transforms/value_transform.py:490`,
   `state.py:7393`) already exclude `any` before the call, so their verdicts do not change
   (inferred; the unit tests for those callers confirm it). The guarantee-walk abstention stays where
   it is, in `resolved_guarantee_type_mismatch`, as an explicit arm decision rather than a
   type-pair rule.
2. **Composer arm selection (medium).** In the `edge` mode of `_resolved_producer_field_type`, split
   the walk the way the runtime does:
   - **Declared arm.** The field is in the runtime model of the effective producer: a fixed or
     flexible source's fields; the probe transform's `output_schema.model_fields` when that model is
     typed, not observed; a union coalesce's merged fields; gates are transparent. Here `any` is a
     known type, compared through the shared rule.
   - **Guarantee arm.** The field is reached through an under-declaring or observed forwarder, or
     carried by a stamp entry on an observed output. Here the current abstention stays.

   Read membership from the probe's model, never from `_output_schema_config`. Otherwise field
   mapper placeholders (c6) become false reds. The comparison stays on base-type names, which keeps
   the composer's deliberate nullability choice. The r2 divergence is a separate question.

   This is the "type resolver with `_edge_field_type_conflict`" seam that the risk assessment names
   as an extraction target. Do it on that touch.
3. **Guidance (small).** Add one line to the `edge_field_type_incompatible` guidance
   (`web/composer/tools/generation.py:1608-1611`): when the producer emits `any`, keep the
   consumer's type and narrow with type_coerce; widening to `any` erases the contract. Make
   `pipeline_capabilities.md:355-357` consistent: a typed consumer of an `any` field is refused at
   build time. Keep both to a few lines.
4. **Agreement matrix (medium).** Add a new parametrized agreement test: producer type (`str`,
   `int`, `float`, `bool`, `any`) × consumer type (the same five plus optional variants) × topology
   (direct source, typed transform, gate, union coalesce, under-declaring forwarder, observed
   forwarder, value_transform target, field_mapper rename, sink consumer). It asserts the composer's
   verdict equals the runtime's verdict.

**Acceptance evidence.**

- **Must flip to "both refuse"**: c1, c1-opt, c2, c3, c4-control, c8, g1, u1.
- **Must stay "both admit"**: c4, c5, c6 (the placeholder trap), c7 node and sink, r1 (the
  type_coerce repair), consumer-`any`, and the `str` control.
- **Must stay "both refuse"**: the `int` control.
- **Existing tests must hold**: the six `any` agreement tests (union coalesce and LLM authored
  output) and `TestDeclaredTypeBehindForwarderUnionMergeAgreement`.
- **Bug-verification protocol**: revert part 2, and every flip case goes red on the composer
  assertion. Mutate the shared rule so that producer `any` admits `str`, and the flip cases go red
  on both surfaces.
- **Scoped runs, per house rules**:
  - `tests/integration/pipeline/test_composer_runtime_agreement*.py`;
  - `tests/unit/web/composer/` and `tests/integration/web/` (a validation behaviour change);
  - `tests/unit/web/composer/test_generation_response_contracts.py` (guidance wire baseline);
  - the contracts unit tests for `declared_type_name_admits`;
  - the mock-discipline baseline;
  - ruff and mypy over `src/elspeth`.
- **Tutorial canary**: a live run compared against base, per the "tutorial must not break" rule. Read
  [inferred]: the canonical brief (csv url → scrape → LLM summary → drop HTML → JSON) yields `str`
  fields, plus `_usage: any`. The planner would have to type a sink field over `_usage` concretely
  to hit the new refusal, and the skill already says `_usage` is `any`. No tutorial-special path is
  added.

**Composer invariants.** This is validation and rejection of what the planner wrote. The server
authors no structure; the remedy text only names the plugin. There is no tutorial branch.

**Frequency in real graphs.**

- [read] The shape has occurred live: session 891b7b1e hit it.
- [read] LLM outputs are not loosely typed in the model. ADR-050 D12 binds LLM structured outputs
  to concrete types, the response is `str`, and only `_usage` is `any`. In a large graph with
  about 12 LLM nodes, the likelier sources of `any` are: json_explode and blob_json_expand elements,
  value_transform targets that cannot be proved (c8: the common post-processing step after an LLM),
  extraction plugins, and `any` that operators write themselves (which the skill encourages for
  "varying types").
- [read, regex] 11 of 231 shipped example YAML files declare an authored `: any` field. None is
  known to reach a typed consumer; examples are exercised, so they build.
- [unknown] No production graph contents were available. Do not assume a rate.

**Size**: medium overall [inferred]. Parts 1 and 3 are small; parts 2 and 4 are medium.

## Open questions for the maintainer

1. **Should the runtime keep two meanings of `any`?** c4 is green and c4-control is red: declaring
   more flips the verdict.
   - Option (a): make authored `any` a known type in the guarantee walk too. That restores
     monotonicity and gives one meaning, but newly refuses YAML pipelines of the c4 shape, so it is
     a compatibility decision.
   - Option (b): accept the asymmetry and document it in ADR-050.

   The recommendation above is unaffected either way: the composer copies whichever rule the runtime
   keeps.
2. **Should a placeholder `any` be distinguishable from an authored one?** `declare_missing_guaranteed_fields`
   writes the same `FieldDefinition("any")` an operator would. If question 1 goes to (a), the walk
   needs that distinction to keep abstaining on placeholders.
3. **Nullability divergence (r2).** The runtime refuses `str | None` into `str`; the composer
   compares base types and says valid. Is that a separate ticket, and in which direction?
4. **Release placement.** This is a composer-only verdict change with no effect on runnable YAML.
   0.8.17, or later?
5. **Skill wording.** `pipeline_capabilities.md:357` tells the planner to "use `any` for created
   fields with varying types". Should it also say, in the same place, that a typed consumer of such
   a field needs a type_coerce step?
