# A/B Experiment — Fork One Case Study To Two LLM Arms

Demonstrates a paired A/B experiment over real LLM calls: every case study is
forked to two assessment arms, a `row_union` barrier releases the pair as one
indivisible group, and a batch comparison reports the cross-arm statistics.

Three configs ship here. The first two isolate one factor each; the third
shows what a lost arm costs.

| Config | Cases | What it varies | Ends |
|--------|-------|----------------|------|
| `settings.yaml` | 8 | **the prompt** — terse rubric vs weighted rubric, one model | COMPLETED, exit 0 |
| `settings_models.yaml` | 8 | **the model** — `analyst-v1` vs `analyst-v2`, one prompt | COMPLETED, exit 0 |
| `settings_arm_loss.yaml` | 24 | nothing new — it makes 3 cases **lose one arm** | PARTIAL, **exit 1 by design** |

An A/B whose arms differ in two ways measures neither, so each of the first two
files changes exactly one thing. In `settings_models.yaml` the only differing
option key between `assess_a` and `assess_b` is `model:`.

## What This Shows

```
source (8 cases) ─(routed)─> [variant_fork] ─┬─ arm_a ─> assess_a ─> tag_a ─┐
                                              └─ arm_b ─> assess_b ─> tag_b ┤
                                                                [variant_union]
                                     ─(experiment_in)─> [prompt_experiment] ─> output
```

8 case studies in, 16 assessed rows unioned out, one comparison row written.

## Relationship to `examples/row_union_ab_experiment`

That example demonstrates the same barrier with deterministic expressions in
place of the arms, and says so in its own header: *"in production the two
branches would be LLM calls with different prompts."* This example is that
one with the LLM calls put back in. Read `row_union_ab_experiment` for the
barrier semantics in isolation; read this one for the shape you would actually
deploy.

## Running

```bash
./examples/ab_llm_experiment/run.sh
```

The launcher starts ChaosLLM on port 8199 with this directory's
`chaos_config.yaml`, runs all three configs against it, verifies each, and
stops the server. It refuses to start if port 8199 is already bound, so do not
run a shared ChaosLLM alongside it.

Every run is deterministic, and each config's expected exit code is asserted
individually: 0 for the first two, **1 for `settings_arm_loss.yaml`**, whose
PARTIAL outcome is the point. An unexpected exit code is a real defect, not
injected-fault noise.

## Output

- `output/prompt_experiment.json` — cross-prompt comparison (one row)
- `output/model_experiment.json` — cross-model comparison (one row)
- `output/arm_loss_experiment.json` — the fail-closed run's comparison
- `runs/audit.db`, `runs/audit_models.db`, `runs/audit_arm_loss.db` —
  separate Landscape trails

Expected figures, reproducible run to run because the mock server's scoring is
deterministic:

| Config | Baseline mean | Variant mean | Delta | Lift | Pairs |
|--------|---------------|--------------|-------|------|-------|
| `settings.yaml` | control 62.62 | treatment 70.12 | +7.50 | 12.0% | 8 of 8 |
| `settings_models.yaml` | analyst-v1 62.50 | analyst-v2 71.50 | +9.00 | 14.4% | 8 of 8 |
| `settings_arm_loss.yaml` | control 66.38 | treatment 68.95 | +2.57 | 3.9% | **21 of 24** |

## Why The Verification Is Not The Exit Code

A pipeline that scored nothing also exits 0. `run.sh` asserts three facts after
each run, and fails the launcher if any is wrong:

1. `batch_size` is twice the expected pair count and both arm counts equal it —
   the `row_union` released only WHOLE pairs, rather than passing single tokens
   through. For `settings_arm_loss.yaml` that count is 21, not 24.
2. `variant_field` is the intended discriminator for that config.
3. The Landscape trail holds the expected number of `call_type = 'llm'`
   records. Every arm genuinely called the provider; nothing was templated
   server-side. In the arm-loss run this is 45 against 42 admitted
   observations, and the launcher reports the three discarded calls explicitly.

## Why `row_union` And Not `coalesce` Or A Queue

**Not `coalesce`.** Coalesce is an N-to-1 *field* merge. It would collapse the
two assessments into one wide row (`arm_a_score` and `arm_b_score` as separate
columns), which the batch statistics plugins cannot consume — they need one row
per observation with a discriminator column. `row_union` preserves cardinality
instead of collapsing it.

**Not a queue.** A queue is an *uncorrelated* fan-in: it interleaves whatever
arrives, with no guarantee that a case's two assessments stay together. A batch
boundary could then fall between a case's control and treatment rows.
`row_union` correlates on `row_id` and releases the pair as one unit.

## The Arms Are Fail-Closed With Each Other — And `settings_arm_loss.yaml` Proves It

If one arm is lost, the `row_union` cannot form the pair, and **the surviving
sibling is invalidated too**. The row is the unit, not the arm.

The first two configs *document* that rule without ever exercising it — their
fault injection is zeroed, so no arm is ever lost and the rule never fires. A
behaviour that never arises is not evidence that it works, so
`settings_arm_loss.yaml` makes it arise deterministically: 24 cases, three of
which carry no `exposure_aud` key at all, and arm B computes an exposure weight
before it will assess anything. Those three arm-B tokens are discarded.

```
baseline_count  21   <- not 24. The three arm-A survivors are invalidated.
variant_count   21
batch_size      42   <- 21 whole pairs, not 45 surviving tokens
llm calls       45   <- 24 on arm A, 21 on arm B
```

`baseline_count` is the tell, and the 45-vs-42 gap is the price. Arm A ran to
completion for all three affected cases — the provider was called, the model
answered, the score was parsed, the token was tagged — and that work is
discarded anyway, because its sibling never arrived. **Three LLM calls are paid
for and thrown away, and that is the correct outcome.** Admitting a one-armed
observation would silently bias the comparison toward whichever arm survives
more often, which is a different experiment wearing this one's name.

The loss is recorded, not merely absorbed: `group_losses` carries three rows
naming the closer (`variant_union`), the group, the lost token and the reason.

```bash
sqlite3 examples/ab_llm_experiment/runs/audit_arm_loss.db \
  "SELECT closer_name, reason, COUNT(*) FROM group_losses GROUP BY 1, 2"
```

This is also why the clean configs zero fault injection: an injected 429 there
would not degrade the experiment, it would end it. Terminal-fault behaviour is
the subject of `chaosllm_endurance`.

### Do not route a lost member to a sink from inside a union branch

All three configs use `on_error: discard` on the transforms inside the arms,
never `on_error: <sink>`. A sink route there builds a DIVERT edge, and the
graph builder warns about it by name (`DIVERT_ROW_UNION_GROUP_LOSS`): the group
fails closed regardless, so the sink promises a recovery path the barrier will
not honour. `discard` states the truth — the token is gone, and the group
machinery settles the consequence.

Relatedly, a sink cannot sit inside a bound region at all: the builder rejects
it flat, because no token may leave a bound region except through its closer.

## Why The Mock Server Uses `template` Mode, Not `preset`

A preset bank cycles canned responses without reading the request, so both arms
would receive the *same* answers and any measured lift would be an artefact of
file order — a fixture that looks like an A/B and measures nothing.

ChaosLLM's `template` mode hands the Jinja2 template `model` and `messages`, so
`chaos_config.yaml` returns a score that genuinely depends on which model was
called and which prompt was sent. The score is derived from the rendered
message length rather than a random draw, so the reported statistics reproduce
exactly on a re-run.

## Trigger Constraint

An aggregation downstream of a `row_union` may only use the implicit
end-of-source trigger (`trigger: {}`). A `count` / `timeout` / `condition`
trigger could fire between a case's two arms and split the group, so the graph
builder rejects it at build time.

## Moving This To A Real Provider

Point both arms at a real endpoint and drop the mock:

```yaml
options:
  provider: openrouter
  api_key: ${OPENROUTER_API_KEY}
  model: openai/gpt-4.1-mini      # arm A
  # model: anthropic/claude-3-haiku   # arm B
```

Remove `base_url`, and expect a real provider's variance: with a live model the
run is no longer deterministic, and `temperature: 0.0` reduces but does not
eliminate cross-run drift. Budget for the fail-closed pairing — every retry
exhaustion costs you both arms of that case, not one.

## See Also

- `examples/row_union_ab_experiment` — the same barrier without LLM calls,
  including the screened-at-settlement variant that shows orphaned siblings
  failing closed
- `examples/fork_coalesce` — the N-to-1 field merge this example deliberately
  does not use
- `examples/chaosllm_endurance` — terminal-fault behaviour under load
