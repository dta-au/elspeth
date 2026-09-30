# K062 fix review (red team): Jinja CPU and memory bound

- Reviewed tree: release/0.8.1 at HEAD ffd704d1a (fix commits 7428603bf, a00ee663a, bfc8181c0, 19467c133, f4cad2f4a, 35a659efc, cec1cdea2, 310ef19c9, e6378dd50, b653a81d4, f3aa6d107)
- Reviewer: red-team subagent, 2026-09-24. Posture: read-only on the main checkout. Mutations ran only in a throwaway detached worktree, which has since been removed.
- Probe scripts and logs are in the session scratchpad, not in the repo.

## Verdict: NOT CLOSED

The render-time bound is real. Rendering now runs in a spawned child process with a CPU limit of about 2 s (RLIMIT_CPU), a 5 s wall-clock timeout, 256 MB of address-space growth (RLIMIT_AS), a 4 MB output cap and an 8 MB context cap. Nested loops, `row.text * N`, `'%0Nd' % x`, joins over large ranges and recursive macros all fail as value-free `TemplateError` row errors, and no worker processes are left behind.

The mechanism the verifier called decisive is still reachable from an authenticated web request. That mechanism is compile-time constant folding of an authored expression inside the web process, which holds the GIL. The fix disabled folding in only one of Jinja's code generators and one of its folding sites:

1. `jinja2.meta.find_undeclared_variables` builds its own `TrackingCodeGenerator(CodeGenerator)`. That generator ignores `code_generator_class` and folds every `{{ }}` output. ELSPETH calls it on web-authored templates, and on the composer path this happens with no Pow check first.
2. `visit_EvalContextModifier` (`{% autoescape <expr> %}`) folds its argument even under `_NoFoldCodeGenerator`, which overrides only `_output_child_to_const`.

The finding's own payload, `{{ (3**(3**16)) % 7 }}`, still stalls the web event loop for 16 s through `POST /api/sessions/{id}/state/yaml`.

## Q1: Compile and render sites (enumeration)

| Site | Path | Bounded? |
|---|---|---|
| `plugins/infrastructure/templates.py:335-359` `_BoundedEnvironment.from_string` | source caps, 2048-node AST cap, Pow ban, no-fold compile in-process, then render in a worker | Render: yes. Compile: NO for `{% autoescape expr %}` (F2) |
| `templates.py:494-537` `SandboxedTemplate` (LLM `PromptTemplate`, RAG query, LLM source) | same as above | same as above |
| `templates.py:540-553` `find_runtime_unbound_variables` -> `jinja2.meta.find_undeclared_variables` | stock `TrackingCodeGenerator`, which folds | **NO** (F1) |
| `web/composer/state.py:4035-4052` `_parse_template_names` (called at state.py:3962, 4132, 4147, 4294 from `_validate_with_probe_cache` at :7600; and at `web/execution/_validation_diagnostics.py:807`) | `env.parse` (source caps only, **no Pow ban**) then `find_runtime_unbound_variables` | **NO** (F1): Pow folds |
| `plugins/transforms/llm/base.py:781` LLMConfig model validator | `find_runtime_unbound_variables(env.parse(...))`. The Pow ban runs earlier in the field validator, but `*` / filters fold | **NO** for `'x' * N` (F1) |
| `plugins/sources/llm/config.py:117-122` | `PromptTemplate(value)`, then stock `find_undeclared_variables(environment.parse(value))` | **NO** for `'x' * N` (F1, by code reading; same call as base.py) |
| `plugins/sinks/azure_blob_sink.py:348-350, 486-487` | `create_sandboxed_environment().from_string(blob_path)` | Render: yes. Compile: NO for autoescape (F2) |
| `plugins/sinks/aws_s3_sink.py:251-280` | raw `SandboxedEnvironment`, but the AST is allowlisted to TemplateData plus `run_id`/`timestamp` Names before `from_string`, and a 4096 B cap | Bounded by construction. Not a gap |
| `core/templates.py:124-141` field extraction | `Environment(autoescape=True).parse` after `validate_jinja_source` | Parse only, no folding. Not a gap |
| `web/plugin_policy/coverage.py:283`, `web/composer/service.py:70` | `extract_jinja2_field_usage` / `extract_jinja2_fields` | Parse only. Not a gap |
| `compile_expression`, `get_template`, direct `jinja2.Template(...)` | grep across src/elspeth: no callers | n/a |

## Findings

### F1 (HIGH, confirmed): compile-time folding via `find_undeclared_variables` still stalls the web process
- Files: `src/elspeth/plugins/infrastructure/templates.py:550`, `src/elspeth/web/composer/state.py:4049-4052`, `src/elspeth/plugins/transforms/llm/base.py:781`, `src/elspeth/plugins/sources/llm/config.py:122`, `src/elspeth/web/execution/_validation_diagnostics.py:807`
- Mechanism: `jinja2/meta.py:53` `TrackingCodeGenerator(ast.environment).visit(ast)` goes through stock `CodeGenerator.visit_Output` and then `_output_child_to_const`, which calls `BinExpr.as_const`. `intercepted_binops` is empty, so `**` and `*` fold. `_BoundedEnvironment.parse` checks only source size; the Pow ban lives only in `from_string`.
- Evidence (scratch `probe_meta.py`, heartbeat thread plus maxrss, `ulimit -v 4000000`):
  - `find_runtime_unbound_variables(create_sandboxed_environment().parse("{{ (3**(3**15)) % 7 }}{{ row.a }}"))`: 3.20 s, **heartbeat max gap 2.53 s** (GIL held)
  - `_parse_template_names("{{ (3**(3**15)) % 7 }}...")`: 3.31 s, gap 2.51 s. At 3**(3**17): **92.93 s, heartbeat gap 91.99 s**. The cost grows super-linearly with a 22-character expression.
  - `LLMConfig(prompt_template="{{ ('x' * 1500000000)|length }}{{ row.a }}", ...)`: **constructed OK**, maxrss 22 MB -> 1564 MB
- Route-level repro (scratch `probe_route.py` reuses `tests/unit/web/sessions/test_routes.py::_make_app`, POSTs YAML with an llm node, and polls `/api/health` every 20 ms):
  - benign `{{ row.a }}`: import 0.04 s, worst poll cycle 0.00 s
  - `{{ (3**(3**15)) % 7 }}`: import 200 in 5.14 s, **worst event-loop cycle 2.56 s**
  - `{{ (3**(3**16)) % 7 }}`: **import 504 after 16.10 s, worst event-loop cycle 16.08 s**. The review-debt timeout fired only after the GIL was released.
  - Attribution: monkeypatching `elspeth.web.composer.state.find_runtime_unbound_variables` to a constant brings the 3**15 import back to 0.04 s with a 0.02 s cycle.
- Impact: this is the exact High-severity K062 DoS. One authenticated YAML import, or one planner-authored `prompt_template`, freezes the single-process web replica (event loop, health probes, every user) for a time the attacker chooses.

### F2 (MEDIUM, confirmed): `{% autoescape <expr> %}` folds in-process despite `_NoFoldCodeGenerator`
- Files: `src/elspeth/plugins/infrastructure/templates.py:75-85` (the override covers only `_output_child_to_const`) and `:354` (`super().from_string(ast)` compiles in the parent). The fold itself happens at `jinja2/compiler.py:1982` `visit_EvalContextModifier`.
- Evidence (scratch `probe_autoescape.py`):
  - `SandboxedTemplate("{% autoescape ('x' * 1500000000)|length > 0 %}{{ row.a }}{% endautoescape %}")`: constructed, **maxrss 132 MB -> 1562 MB** in the parent
  - Controls: the same `'x' * 1500000000` in a plain `{{ }}` gives 0.00 s and no RSS growth, and Pow is rejected
  - CPU: `('x' * 200000000)|wordcount` inside autoescape takes 0.47 s per block and 4.73 s for 10 blocks (linear). The `{%` cap of 64 allows 32 blocks, and N is limited only by host memory.
- Reachable wherever `from_string` runs in the web process: the LLMConfig field validator (composer probe and YAML import), RAG `query_template`, the LLM source, and the Azure blob `blob_path`. Parent memory is not bounded by RLIMIT_AS (only the child is), The GIL is released between filter calls, so the CPU cost is a sustained burn, not a single long loop stall like F1. N was capped at 1.5 GB here under `ulimit -v`. A larger N (for example `'x' * 30000000000`) is expected to OOM-kill the single-process replica, which means process death, not a stall. That is probable and was NOT run.

### F3 (MEDIUM, confirmed at the render layer, probable at the row layer): one spawned process per render; benign rows fail under concurrency
- Files: `src/elspeth/plugins/infrastructure/templates.py:50-56, 254-323`; routing at `src/elspeth/plugins/transforms/llm/transform.py:297-307`, `:640-650`, and `rag/query.py:125-136`
- Every non-literal render starts a fresh `spawn` interpreter, and a process-global `BoundedSemaphore(2)` limits it to two at a time.
- Measured (scratch `probe_perf.py`, 24 CPUs, load about 7): a trivial `Classify: {{ row.a }}` render takes a **median of 707 ms** (670-747 ms over 20 renders). With 16 threads x 64 renders: 2.7 renders/s and a maximum latency of 23.3 s. With 32 threads x 128 renders: 3.5 renders/s and **30 of 128 benign renders failed with `TemplateError: Too many concurrent template workers`** (30 s queue timeout).
- These failures are load-induced, but the transform routes them as `template_rendering_failed` row errors, the same bucket as a real template defect. `pool_size` has no upper bound (`llm/base.py:316`). The 5 s wall timeout also counts spawn start-up, so a loaded host can fail benign rows with "execution time limit" (probable, not measured).
- Corroboration: 310ef19c9 raised a flush timeout from 30 s to 90 s, f3aa6d107 raised another from 30 s to 120 s ("worker startup can exceed 30 seconds"), and e6378dd50 replaced templated prompts with the static `"Evaluate"` string so load tests skip the worker.

### F4 (MEDIUM, confirmed): mutation survivors; the compile-time and memory bounds are untested
Worktree at ffd704d1a, detached. `elspeth.__file__` and `elspeth_lints.__file__` both resolved inside the worktree. Focused set: `tests/unit/plugins/infrastructure/test_templates.py`, `tests/unit/plugins/llm/test_templates.py`, `test_llm_config.py::TestLLMConfigBase::test_config_validation_rejects_constant_power_template`, `test_app.py::TestHealthEndpoint::test_health_responds_during_malicious_jinja_render`, `test_routes.py::TestYamlEndpoint::test_yaml_template_validation_offloads_while_health_responds`. Run with `-n 0 -p no:cacheprovider`. Baseline: 108 passed, exit 0.

| Mutant (templates.py) | Result | Killer |
|---|---|---|
| M1 Pow ban -> `if False:` | killed (exit 1) | `test_constant_power_is_bounded_during_configuration`, `test_config_validation_rejects_constant_power_template`. They pin the error message; with M1 the no-fold codegen still prevents the stall |
| **M2 `code_generator_class = CodeGenerator` (folding restored)** | **SURVIVED** (108 passed) | none. A grep across tests/ finds no reference to `_NoFoldCodeGenerator` or to folding |
| M3 drop RLIMIT_CPU | survived | the 5 s wall timeout covers it (redundant bound) |
| M4 wall timeout 5 s -> 600 s | survived | RLIMIT_CPU covers it. The nested-loop test regex accepts either outcome |
| **M5 drop RLIMIT_AS** | **SURVIVED** (108 passed) | none. `row.text * 300000000` still trips the 4 MB output cap. No test allocates without output |
| M6 output cap -> `if False:` | killed | `test_render_output_is_bounded` |
| M7 render in-process (`return compiled`) | killed only by a hang (timeout exit 124) | nested-loop tests hang; several value-free tests fail |

Test that passes for the wrong reason: `test_yaml_template_validation_offloads_while_health_responds` checks health only while a worker thread is parked on an Event, so it never measures the event loop while validation is folding. Its own payload, `{{ (3**(3**15)) % 7 }}`, stalls the event loop for 2.56 s on the same route (F1 repro) and the test still passes.

### F5 (LOW, confirmed by diff): a retry-proof assertion became vacuous
- `tests/unit/plugins/llm/test_azure_multi_query_retry.py` (f3aa6d107): `assert call_count[0] > 1` ("Proof that retry happened") was changed to `>= 1`, which any single call satisfies. The comment defers to "the separate recovery test". The underlying cause is the worker start-up cost in F3.

## Q3: Failure mode, lifecycle and value-freeness (attacks that did NOT break it)
- Scratch `probe_render.py`, main-guarded, `ulimit -v 8000000`. All of the following became `TemplateError`, which the LLM and RAG transforms route as `TransformResult.error({"reason": "template_rendering_failed", ...})`, a Tier-2 row error:
  - nested `range(100000)` x2 -> "Template worker stopped before completing" after 2.90 s (killed by RLIMIT_CPU)
  - `row.text * 300000000` -> "exceeded the memory limit"
  - `%0<row>d` width -> memory limit
  - `range|map|join(sep*1000)` -> withheld TemplateError
  - recursive macro -> worker stopped
  - `row.text.encode(row.enc)` (LookupError) and `row.fmt.format()` (KeyError) -> worker stopped
- Landscape: `transform_errors` stores the reason (see the `withheld_error_detail` docstring). The text that reaches it is the value-free `TemplateError` string shown above; I did not trace the DB write itself.
- A marker row value (`SECRET-ROW-VALUE-42`) appeared **0 times** in the error text or in the parent's stderr. Uncaught child exceptions print only "Process SpawnProcess-N:".
- Children after each render: only the singleton `multiprocessing.resource_tracker`. There were no orphaned or zombie workers after the probes (checked with `ps` for `multiprocessing.spawn`). The `finally` block kills and joins the child on every path.
- Caveat: a caller script with no `if __name__ == "__main__"` guard gets a non-TemplateError `RuntimeError` from spawn bootstrapping, which would escape the transform's `except TemplateError`. The elspeth CLI and uvicorn entry points are guarded, and spawn was already used in rag/query.py and rasterize, so this is not a new hazard and is not filed.

## Q4: Performance
See F3: about 0.7 s of spawn per row render, and at most two in flight per process, so roughly 3 renders/s per process whatever `pool_size` is set to. A 10k-row LLM pipeline pays about 1 h of template overhead. Static-text templates skip the worker, which is why e6378dd50 switched the load tests to `"Evaluate"`.

## Suggested direction (not implemented)
Stop calling stock `jinja2.meta.find_undeclared_variables`, or run it with a `TrackingCodeGenerator` subclass that raises `Impossible` in `_output_child_to_const`. Also override `visit_EvalContextModifier` in `_NoFoldCodeGenerator` so it never calls `as_const`, or reject `ScopedEvalContextModifier`/`EvalContextModifier` nodes. Enforce the Pow ban (and any AST checks) in `parse()`, not only in `from_string()`. Add tests that fail if folding returns, for example by asserting that `SandboxedTemplate` construction and `_parse_template_names` on `{% autoescape ('x'*10**9)... %}` / `{{ 'x'*10**9 }}` keep RSS flat. Add an allocation-without-output test for RLIMIT_AS. Treat capacity and timeout failures as retryable or operational rather than as template defects, and consider a persistent worker pool.

```json
{"findings": [
  {"title": "Compile-time constant folding still reachable via jinja2.meta.find_undeclared_variables: one YAML import stalls the web event loop 16s+ (K062 primary mechanism not closed)",
   "severity": "high",
   "confidence": "confirmed",
   "files": ["src/elspeth/plugins/infrastructure/templates.py", "src/elspeth/web/composer/state.py", "src/elspeth/plugins/transforms/llm/base.py", "src/elspeth/plugins/sources/llm/config.py", "src/elspeth/web/execution/_validation_diagnostics.py"],
   "repro": "PYTHONPATH=/home/john/elspeth/src timeout 60 /home/john/elspeth/.venv/bin/python -c \"from elspeth.web.composer.state import _parse_template_names; _parse_template_names('{{ (3**(3**15)) % 7 }}{{ row.a }}')\" (about 3s, GIL held about 2.5s; 3**(3**17) held the GIL 92s). Route level: build _make_app from tests/unit/web/sessions/test_routes.py, POST /api/sessions/{id}/state/yaml with an llm node whose prompt_template is '{{ (3**(3**16)) % 7 }}' while polling /api/health: 504 after 16.10s, event-loop cycle 16.08s (benign template: 0.04s). Monkeypatching composer.state.find_runtime_unbound_variables removes the stall.",
   "detail": "find_runtime_unbound_variables (templates.py:550) and sources/llm/config.py:122 call stock jinja2.meta.find_undeclared_variables, whose TrackingCodeGenerator subclasses the stock CodeGenerator (not _NoFoldCodeGenerator), so visit_Output folds BinExpr constants. The composer path _parse_template_names (state.py:4049-4052) uses env.parse, which carries no Pow ban. LLMConfig base.py:781 folds 'x'*N (1.5GB RSS measured). This is the exact High-severity DoS K062 described: one authenticated request freezes the single-process web replica for an attacker-chosen time."},
  {"title": "{% autoescape <expr> %} is constant-folded in the web process despite _NoFoldCodeGenerator (unbounded parent memory/CPU at config time)",
   "severity": "medium",
   "confidence": "confirmed",
   "files": ["src/elspeth/plugins/infrastructure/templates.py"],
   "repro": "(ulimit -v 4000000; PYTHONPATH=/home/john/elspeth/src timeout 20 /home/john/elspeth/.venv/bin/python -c \"from elspeth.plugins.infrastructure.templates import SandboxedTemplate as S; S(\\\"{% autoescape ('x' * 1500000000)|length > 0 %}{{ row.a }}{% endautoescape %}\\\")\") : maxrss 132MB -> 1562MB in the parent; ('x'*200000000)|wordcount blocks cost 0.47s each, linear, up to 32 blocks.",
   "detail": "_NoFoldCodeGenerator overrides only _output_child_to_const; jinja2 compiler.py:1982 visit_EvalContextModifier still calls keyword.value.as_const, and _BoundedEnvironment.from_string compiles in-process (templates.py:354). This is reachable from LLMConfig/RAG/LLM-source validation and the Azure blob_path. RLIMIT_AS applies only to the render child, so the web process's memory is unbounded. A larger N is expected (probable, not run) to OOM-kill the replica."},
  {"title": "Per-render spawn (~0.7s) plus a 2-slot global semaphore: benign rows fail with 'Too many concurrent template workers' under concurrency and are routed as template defects",
   "severity": "medium",
   "confidence": "confirmed",
   "files": ["src/elspeth/plugins/infrastructure/templates.py", "src/elspeth/plugins/transforms/llm/transform.py", "src/elspeth/plugins/transforms/rag/query.py"],
   "repro": "PromptTemplate('Classify: {{ row.a }}').render({'a':1}) x20 serial: median 707ms. 32 threads x 128 renders: 3.5 renders/s, 30/128 raised TemplateError 'Too many concurrent template workers' (scratch probe_perf.py, main-guarded).",
   "detail": "templates.py:254-323 spawns a fresh interpreter per render behind BoundedSemaphore(2) with a 30s queue timeout and a 5s wall timeout that includes spawn start-up. Transform code (transform.py:297-307, 640-650) records these load-induced failures as template_rendering_failed row errors. pool_size has no upper bound. Commits 310ef19c9, e6378dd50 and f3aa6d107 loosened or bypassed tests because of this cost."},
  {"title": "Mutation survivors: restoring Jinja constant folding and removing RLIMIT_AS are killed by no test; the YAML-route bound test passes while its payload stalls the loop",
   "severity": "medium",
   "confidence": "confirmed",
   "files": ["src/elspeth/plugins/infrastructure/templates.py", "tests/unit/plugins/infrastructure/test_templates.py", "tests/unit/plugins/llm/test_templates.py", "tests/unit/web/sessions/test_routes.py"],
   "repro": "Detached worktree at ffd704d1a, PYTHONPATH=<wt>/src:<wt>/elspeth-lints/src, pytest on the focused set -n 0 -p no:cacheprovider (baseline 108 passed). Mutant code_generator_class = CodeGenerator: 108 passed. Mutant dropping resource.setrlimit(RLIMIT_AS,...): 108 passed. grep of tests/ finds no reference to _NoFoldCodeGenerator or to folding.",
   "detail": "The compile-time bound is pinned only by a Pow-message test (which a blocklist satisfies), and the memory rlimit is masked by the 4MB output cap in the only allocation test. test_yaml_template_validation_offloads_while_health_responds does not measure the loop during validation; the same payload stalls the event loop for 2.56s on that route. RLIMIT_CPU and the wall timeout each survive removal individually (redundant bounds)."},
  {"title": "f3aa6d107 made the retry-proof assertion vacuous (call_count > 1 -> >= 1)",
   "severity": "low",
   "confidence": "confirmed",
   "files": ["tests/unit/plugins/llm/test_azure_multi_query_retry.py"],
   "repro": "git show f3aa6d107 -- tests/unit/plugins/llm/test_azure_multi_query_retry.py",
   "detail": "The assertion labelled 'Proof that retry happened' now accepts a single call; the weakening was driven by template-worker start-up consuming the 1s retry budget (see the spawn-cost finding)."}
]}
```
