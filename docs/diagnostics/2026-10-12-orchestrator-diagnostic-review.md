# Orchestrator diagnostic review — 2026-10-12

**Repair update:** the cleanup P2 below is now repaired in the local combined
branch. The original review evidence and source coordinates are retained below;
the final section describes the implementation and bounded validation.

**One new analogous diagnostic finding: engine cleanup discards stable node
attribution and causal metadata. No new Composer convergence or graph-edit
correctness defect was demonstrated in this bounded review.** The Composer
review confirms the known evidence-loss boundary and an explicitly documented
multi-turn advisor-context limitation; neither establishes the historical
incident's cause. Source and tests were not changed by this review.

## Scope and method

Reviewed the working tree on `codex/emergency-diagnostics-20261012`, base
`fdaa670394fd311fc5df61befb1a7b1941c01182`, with the local D29/D30 and D21
repairs already present. “Orchestrator” resolves to two concrete layers:

- Composer: `service.py` chooses planner versus iterative authoring at
  1056–1106; `_compose_loop` at 2036 coordinates model, dispatch, persistence,
  completion and budget phases. `planning_application.py` stages planner
  outcomes; `pipeline_planner.py` owns bounded planning, repair and discovery.
- Engine: `engine/orchestrator/core.py:88,180,245` delegates run execution to
  `RunLifecycleCoordinator`; lifecycle, leader-drain, resume and cleanup
  modules own primary-error propagation and teardown.

Applied the explicitly requested `using-llm-specialist` and
`superpowers:systematic-debugging` skills. Both named entry paths existed;
no fallback was needed. Loaded the agentic/MCP, context-window-management and
reasoning-model reference sheets plus systematic root-cause tracing. Used
those sheets to examine tool/result pairing, finite budgets, context
continuation and failure accounting. Provider recommendations in the sheets
were not treated as evidence of this application's behavior. No model-quality,
hidden-reasoning, latency or production-causation inference is made.

The review traced selected failure boundaries through source and existing
focused tests. One pure synthetic cleanup control ran with explicit cwd and
both intended source roots, verified imports, and exited 0. No broad suite,
paid/live provider call, credential/state inspection, SQL/image work, source
or test edit was performed. The D29/D21 source freeze was preserved.

## New finding

**P2 — Cleanup failures cannot reliably identify the failing pipeline node;
secondary causal location is also discarded.**

`src/elspeth/engine/orchestrator/cleanup.py:165–178` receives each hook's
`node_id`, scopes `PluginContext.node_id` for execution, and catches the
failure after the scope has restored the previous context (`40–47`). It then
calls `record_cleanup_error` without the node ID. That recorder (`148–162`)
keeps run ID, hook, plugin name, outer exception class, scrubbed preview,
message digest and length. It retains no node ID, safe frame coordinates,
cause classes or exception object. Multiple instances of the same transform
legitimately share their plugin name, e.g. `passthrough`
(`src/elspeth/plugins/transforms/passthrough.py:41`).

On otherwise successful execution, `cleanup.py:236–252` raises a new
`RuntimeError` built only from those strings. On an existing failure it logs
the string aggregate and preserves the primary exception for ordinary
cleanup failures. Tier-1 corruption errors deliberately bypass that guard
and propagate immediately. Thus ordinary primary-error behavior is correct;
the new finding concerns the cleanup
failure's own attribution and causal evidence. A hash supports correlation
but cannot recover the failing frame or the wrapped cause.

This is not merely a kwargs-only logging observation. The synthetic control
used the actual `core/logging.py:139–185` JSON processor chain and stdlib
`ProcessorFormatter`. A bound request ID was merged successfully; node IDs
were absent from rendered cleanup records. `plugin_node_scope` changes the
context object, not structlog contextvars. Two distinct same-plugin nodes
produced equal failure records except timestamps; changing which sole node
failed produced the same rendered record. The control also verified that
an in-flight primary exception retained identity and the outer node context
was restored.

| Synthetic control | Observed result |
|---|---|
| Non-failing hook | 0 failure records |
| Two failing same-plugin hooks, distinct node contexts | 2 records; equal except timestamp; no `node_id` |
| First node only fails versus second node only fails | Same rendered failure record except timestamp |
| Production processor known-positive control | Bound `request_id` present |
| Aggregate on otherwise normal return | No `__cause__` or `__context__` |
| Ordinary cleanup failure during primary failure | Primary exception identity preserved |

Caller tracing found no orchestrator-owned replacement failure carrier:
`leader_drain.py:176–178,427–428`, `run_context_factory.py:293–303` and
`resume.py:1430–1435` call this function for side effects or receive its
aggregate exception. This does not exclude individual hooks writing their
own audit records; it limits the finding to the orchestrator's failure record.

Small future repair: thread the already-owned node ID into each failure
record and aggregate; retain bounded safe cause classes and repository-relative
frame coordinates without raw messages, locals or arbitrary paths. Admit frame
locations against trusted repository source rather than treating an arbitrary
exception code filename as trustworthy. Keep the current credential scrub,
ordinary primary-error precedence and immediate Tier-1 propagation. Tests should compare
same-plugin/different-node failures through the real logging processors and
verify attribution, cause metadata, secret exclusion and unchanged primary
exception identity. No remediation was implemented here.

## Existing findings and deliberate projections

| Boundary | Verified interpretation |
|---|---|
| D20 tool evidence | Already reported: normal and alternate audit persistence replace authored payloads with display projections. This review does not reopen the same mechanism as a new finding; the existing design note remains the repair proposal. |
| Planner provider failure evidence | `pipeline_planner.py:3437–3464` records class-only error messages; known provider timeout wrapping at `3600–3601` suppresses the original displayed chain. This overlaps the earlier Composer map. Class-only public output is intentional; a missing private diagnostic trail is the separate supportability concern. |
| Advisor continuation | `service.py:2505–2523` explicitly documents and implements advisor-message elision after the first successful mutation, even if a repair needs later mutating turns. This is a preexisting context limitation, not a newly reproduced incident or proof that a later repair fails. |
| Withheld prose | `composition_completion.py:843–870` explicitly preserves superseded model prose as non-rendered audit rows. Keeping those rows out of later model context is a policy projection, not evidence destruction by itself. |
| Credential handling | Cleanup's `scrub_text_for_audit` plus bounded preview (`cleanup.py:66–84`) deliberately protects public/log surfaces. The new finding does not ask for raw credential or full exception-text persistence. |

Useful working contrasts: Composer's deferred-cancellation helper preserves
child failure as the cancellation's cause (`service.py:183–218`); dispatch
and persistence complete before the safe cancellation checkpoint
(`2346–2474,2584–2590`). Planner discovery cancels and drains siblings before
propagating the primary failure (`pipeline_planner.py:4613–4643`). Engine
partial-result handling restores the original exception and traceback
(`run_lifecycle.py:746–772`). Existing tests explicitly exercise primary
cleanup preservation, credential-safe cleanup output and deferred-cancellation
cause retention; those tests were read, not rerun for this audit.

## Limits

No new D31 atomic-edit defect is asserted: draft mutations may intentionally
permit invalid intermediate graph state, and this review did not establish a
violated contract. No conclusion is drawn about D32 row-context loss, D33
provider convergence or D34 document consultation in the historical session.
Those need reconstructable authored inputs and provider-visible context;
this review cannot recover evidence already discarded under D20.

Synthetic script and exited log are lane-local:
`.claude/lanes/emergency-diagnostics-20261012/orchestrator-cleanup-control.py`
and `orchestrator-cleanup-control.log`. The control proves this narrow
current-source mechanism, not a production incident, full orchestration
correctness, filesystem/crash behavior, deployment or runtime acceptance.

## Local repair and regression evidence

The cleanup recorder now receives the hook's owned node ID directly and includes
it in the per-hook structured log and aggregate. It retains bounded cause class
names and package-relative source coordinates admitted against compiled trusted
ELSPETH source. External/generated frames are withheld; arbitrary source text,
locals, function names and nested exception messages are not exposed. Withheld
and truncated metadata are explicit. Existing message scrubbing and digest
semantics remain intact.

Optional diagnostics bound chain depth, frame count, source reads, cache size and
code metadata traversal. Built-in descriptors and exact compiler-metadata type
admission prevent unusual exception classes or generated code from invoking
callbacks while diagnostics inspect them. Unavailable source coordinates,
including Python 3.12 symlink-loop behavior, do not replace the cleanup error.

Attribution regressions first failed on missing node IDs in real JSON output and
the aggregate. The final cleanup, follower-teardown and resume-failure selection
completed with exit 0: **87 passed**, two existing Typer deprecation warnings.
Ruff, formatting and narrow mypy checks passed. Ordinary primary-exception
identity, continued cleanup, credential exclusion, immediate Tier-1 propagation,
cause cycles, bounds and hostile metadata controls are covered. Whole-tree and
combined delivery qualification are tracked in the
[combined handoff](2026-10-12-combined-repair-handoff.md); these focused results
alone are not a full-suite or deployment claim.
