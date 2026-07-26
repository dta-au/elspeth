# DAG Corpus Wave A Design

**Status:** Approved execution slice from the 2026-07-26 P1 coordination
decision.

## Goal

Advance the maintained DAG scenario corpus in parallel with the deferred
platform program without asserting evidence across runtime surfaces that are
still changing. Wave A repairs the two open evidence-integrity defects and
adds a bounded set of executable configuration-to-production-build cases.

## Boundary

Wave A owns only:

- `elspeth-e8acea2a55`: bind a case's declared input fixture to the path the
  rendered runtime configuration actually consumes;
- `elspeth-d88d0e45c0`: contain documentation-link resolution inside the
  repository;
- the corpus manifest, schema, loader, harness, fixtures, documentation, and
  focused corpus tests; and
- configuration loading, plugin instantiation, production graph construction,
  graph validation, production pipeline-config assembly, and exact graph-shape
  evidence.

Wave A does not change or promote runtime, audit, recovery, checkpoint,
contention, or multi-worker evidence. It does not edit Orchestrator, Landscape,
checkpoint, scheduler, deployment, session, or deferred-platform production
paths. Those cells remain non-pass until the platform program reaches its
stable integration boundary.

## Evidence-integrity decisions

### Runtime-consumed input

`HarnessCaseSpec.input_fixture` remains the one declared source input for the
v1 corpus. The canonical YAML must reference `${input_csv}`. Rendering fails
closed if the placeholder is absent, so the evidence hash cannot include bytes
that the rendered source configuration ignored. The input path and fixture
path continue to resolve through the strict fixture-root containment helper.

This is intentionally narrower than trying to infer arbitrary plugin file
dependencies from loaded settings. A future multi-input corpus extension must
add an explicit typed mapping of input names to fixture paths and require every
declared name to be consumed.

### Repository-relative documentation links

The documentation-link contract resolves decoded relative targets against the
document directory, then proves the resolved target is contained by the
repository root before checking existence. Parent traversal that remains
inside the repository is valid; traversal outside the repository is reported
as invalid even when the external target exists.

## Build-only evidence model

The harness gains a `build` workflow alongside `run` and `recovery`.
Build-only cases have a dedicated expectation containing:

- exact node count;
- exact edge count;
- exact node-type counts; and
- exact sorted edge labels.

Observed graph evidence records the same shape plus the existing canonical
topology hash. A build case returns common `ScenarioRunEvidence` with completed
stages `config` and `build`; runtime, audit, and recovery are explicitly
unattempted zero-value records. Workflow/expectation mismatches fail schema
validation.

## Bounded case set

Wave A adds four deterministic fixtures using repository-owned plugins and the
same contained input file contract:

1. `multiple-independent-sources:independent-roots`
2. `multi-source-queue-fan-in:queued-fan-in`
3. `conditional-routing:two-way-gate`
4. `fork-coalesce-policies:require-all-nested`

Each case crosses the real YAML loader, runtime plugin factory in preflight
mode, `ExecutionGraph.from_plugin_instances`, structural and edge-contract
validation, and `assemble_and_validate_pipeline_config`. These cases strengthen
the executable corpus but do not promote a manifest cell merely because a
nearby layer passes; current status metadata changes only where the exact cell
requirement is fully proven.

## Failure posture

- No skips, xfails, plan references, or documentary evidence may stand in for
  an executable case.
- Missing or unused declared input fixtures fail before settings load.
- Build cases must never call Orchestrator or create Landscape state.
- Exact graph evidence is compared to the declared expectation in the common
  table-driven integration test.
- Existing run and recovery cases retain their current semantics.

## Wave B trigger

Wave B starts only after the deferred-platform plan's stable runtime boundary
is integrated into `release/0.7.2`. It rebases this corpus branch onto that
commit, reruns all Wave A evidence, and then implements runtime, audit,
recovery, checkpoint, contention, and multi-worker cases against the new
authoritative production path. The operator-owned P0 signing pass remains tied
to the final frozen post-platform, post-corpus release SHA.
