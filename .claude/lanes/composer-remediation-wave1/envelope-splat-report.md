# Conditional envelope splat scanner repair

Implementation is ready for parent review. The complete envelope gate and
walker authority passed in the initial selected verification. That run exposed
an additional prior-remediation forwarder-census omission; parent then
explicitly authorized its narrow repair. Final affected census/catalogue/
response verification is green: **664 passed**, exit **0**. The unchanged
envelope scanner retains its previously tested hash. Parent still owns Astra
review, integration, and broad final verification before closing item 12.07.

## Scope and frozen candidate

- Worktree: `/workspace/elspeth/.claude/worktrees/composer-remediation-wave1`.
- Base/HEAD: `ef14c2fc18fba93158ae7a6cfe004a4537c7ef98` throughout.
- Only changed tracked files:
  `tests/unit/web/composer/test_tool_result_envelope_gate.py` and
  `tests/unit/web/composer/test_error_code_redaction.py`.
- Envelope scanner SHA256 (identical across both verification stages):
  `a8b42e436328e7dbcff845d0f3d84310f1c00aef06b7424885349f743369cda7`.
- Error-code census SHA256:
  `14b649597141f1781f658782229eec5bdcf84736bdeb273d669bc1b185398a3b`.
- Review patch: `envelope-splat-review.diff`, SHA256
  `4f884d7245ff3516c8a9e223f43bc369060dfbc38fa8b7eb36b48c40b9c3cecd`.
- Prior D1 structural assertions are unchanged. No production, fence,
  teaching, snapshot, count, registry, signature, or fingerprint edits. The
  only census-inventory additions are the two explicitly reviewed forwarder
  tuples authorized in the brief's appended section. No subagents, commits,
  or full suite. Deliberate untracked frontend `node_modules` remains untouched.

The old 14 failures are our regression from diagnostic commit `f598aa43d`,
present before D1; they are not unrelated upstream failures. The producer's
conditional field omission remains intact. Supporting baseline diagnosis is
`baseline-envelope-gate-analysis.md`; the earlier 14-failure base comparison
remains in `d1-state-envelope-baseline14.log` / `.exit`.

## Change and controls

`_splat_keys` now reads inline `ast.Dict` with the existing dictionary walker
and enumerates both alternatives of `ast.IfExp`. Constants, handler-local
resolver, and `dict_forwarded` context travel through that recursion. The
outer dictionary applies its prefix once. Existing own-result `.data`, named
attributions, helper attributions, and unsupported-expression refusals remain.

New tests cover:

- Inline and conditional mappings, empty and nonempty alternatives, distinct
  branch keys, nested splats/keys, prefixed paths, and emission order.
- Module/imported string constants, handler-local resolution, and a live
  attributed payload helper across conditional branches.
- Dynamic keys, unresolved names without a resolver, unattributed helpers,
  unsupported binary expressions, mapping casts, and cast payload values in
  each alternative. Assertions require the actual branch-specific refusal,
  so the old earlier `IfExp` refusal cannot satisfy them.
- Nested conditional casts under `dict(...)` in all outer/inner branch
  combinations, while predicate/helper-input casts remain permissible.
- The actual generation diagnostic constructor, then source-AST mutations
  planting an untaught key independently in each real conditional arm and
  an envelope homonym (`success`) in each arm. Each test first proves the
  original constructor and gate work, proves the planted key is censused at
  that constructor, then requires the relevant gate to fail for that key.

The authorized follow-on adds exactly two reviewed forwarder tuples for the
diagnostic handler's `error_text` and `matched_code`, with provenance comments:
the first is echoed only after successful exact catalogue lookup; the second
comes only from catalogue keys associated by record identity. Eight controls
prove those two exact sites are admitted and changed expression/function/path
sites remain unreviewed. The catalogue-to-registry subset assertion and census
walker are unchanged. Existing runtime tests retain ambiguous/unowned/unknown
omission, canonical-code ownership, and frozen response bytes.

## Commands and terminal results

All Python commands used explicit worktree cwd and:

```bash
export PYTHONPATH=/workspace/elspeth/.claude/worktrees/composer-remediation-wave1/src:/workspace/elspeth/.claude/worktrees/composer-remediation-wave1/elspeth-lints/src
```

`envelope-splat-provenance.log` records both imported package paths inside
that worktree and the exact HEAD above. Commands below use `.venv/bin/python`.
Each run wrote a lane-private full log and a terminal `.exit` file; the exit
was read before reporting its result.

1. TDD red, tests added before the scanner implementation:

   ```bash
   .venv/bin/python -m pytest tests/unit/web/composer/test_tool_result_envelope_gate.py -n 0 -q -k 'splat or conditional_splat'
   ```

   `envelope-splat-red.log` / `.exit`: exit **1**, **28 failed, 1 passed,
   182 deselected in 5.35s**. Every new case failed at the old inline-Dict or
   conditional-IfExp refusal; the preexisting splat-refusal control passed.

2. Same focused command after the minimal scanner change:

   `envelope-splat-focused.log` / `.exit`: exit **0**, **29 passed,
   182 deselected in 14.74s**.

3. Initial frozen selected verification, after Ruff formatting:

   ```bash
   .venv/bin/python -m pytest tests/unit/web/composer/test_tool_result_envelope_gate.py tests/unit/elspeth_lints/test_python_file_walker_authority.py tests/unit/web/composer/test_validation_guidance_catalogue.py tests/unit/web/composer/test_generation_response_contracts.py tests/unit/web/composer/test_error_code_redaction.py -n 0 -q
   ```

   `envelope-splat-verification.log` / `.exit`: exit **1**, **1 failed,
   877 passed in 111.09s**. This is the initial result, before the authorized
   follow-on. The sole failure is identified below. The full
   envelope gate and all new branch/mutation controls passed; the diagnostic
   catalogue and frozen producer-response byte/admission controls also passed.

   Before/after SHA256 lists for tracked `src`, `scripts`, `tests`,
   `elspeth-lints`, `pyproject.toml`, `AGENTS.md`, and `CONTRIBUTING.md` were
   written with `git ls-files -z ... | xargs -0 sha256sum` to
   `envelope-splat-freeze.before` and `.after`. `cmp` exit **0**, recorded in
   `envelope-splat-freeze.exit`; tested source did not change during the run.

4. Focused reproduction of the additional failure:

   ```bash
   .venv/bin/python -m pytest tests/unit/web/composer/test_error_code_redaction.py::TestRegistryCensus::test_every_non_literal_code_site_is_a_reviewed_forwarder -n 0 -q
   ```

   `envelope-splat-forwarder-repro.log` / `.exit`: exit **1**, **1 failed
   in 2.88s**, with the same two missing forwarders.

5. Follow-on TDD red, new controls before the two reviewed-site additions:

   ```bash
   .venv/bin/python -m pytest tests/unit/web/composer/test_error_code_redaction.py -n 0 -q -k diagnostic_code_forwarder
   ```

   `envelope-forwarder-red.log` / `.exit`: exit **1**, **2 failed, 6 passed,
   76 deselected in 0.51s**. Both exact-site positive controls fail because
   their tuples are missing; all changed-site negative controls pass.

6. Final frozen verification after the two authorized additions:

   ```bash
   .venv/bin/python -m pytest tests/unit/web/composer/test_error_code_redaction.py tests/unit/web/composer/test_validation_guidance_catalogue.py tests/unit/web/composer/test_generation_response_contracts.py -n 0 -q
   ```

   `envelope-splat-final-verification.log` / `.exit`: exit **0**, **664 passed
   in 16.39s**. This includes the formerly failing real-tree census, all eight
   exact/changed-site controls, the stale-entry and catalogue-subset pins,
   every catalogue case, and generation response byte/admission contracts.

   `envelope-splat-final-provenance.log` records the same worktree package
   paths and HEAD. Final source freeze files are
   `envelope-splat-final-freeze.before` / `.after`; their `cmp` exit is **0**
   (`envelope-splat-final-freeze.exit`). The expensive full envelope gate was
   not repeated for this isolated census-test change, as explicitly directed;
   its scanner source hash and all production inputs remain unchanged.

7. Final scoped lint and format checks:

   ```bash
   .venv/bin/python -m ruff check tests/unit/web/composer/test_tool_result_envelope_gate.py tests/unit/web/composer/test_error_code_redaction.py
   .venv/bin/python -m ruff format --check tests/unit/web/composer/test_tool_result_envelope_gate.py tests/unit/web/composer/test_error_code_redaction.py
   git diff --check
   ```

   Ruff exit **0** (`envelope-splat-final-ruff.log` / `.exit`); format exit **0**
   (`envelope-splat-final-format.log` / `.exit`); `git diff --check` exit **0**.
   Initial format check requested formatting; formatter was applied before
   the frozen final verification. No edits followed that verification.

## Additional finding, authorized repair, and remaining verification

The initial
`test_error_code_redaction.py::TestRegistryCensus::test_every_non_literal_code_site_is_a_reviewed_forwarder`
failure reported these source-derived sites missing from `_REVIEWED_FORWARDERS`:

```text
('composer/tools/generation.py', '_execute_explain_validation_error', 'error_text')
('composer/tools/generation.py', '_execute_explain_validation_error', 'matched_code')
```

Before the authorized follow-on,
`git diff --exit-code ef14c2fc18fba93158ae7a6cfe004a4537c7ef98 --
src/elspeth/web/composer/tools/generation.py
tests/unit/web/composer/test_error_code_redaction.py` returned **0**: neither
its source producer nor its census had changed in the scanner-only task. The earlier
`git show f598aa43d -- .../generation.py` introduces both nonliteral
`error_code` values, while the initial inventory contained only the existing
`code` forwarder for this handler. This was a second prior-remediation
diagnostic gate obligation; it was not caused by scanner recursion. Parent
verified the closed provenance and appended explicit authorization to
`envelope-splat-brief.md`. The implementation adds exactly those two reviewed
sites using the census's intended explicit-site workflow; it does not broaden
the walker or permit the same expression at another function/path. The final
664-test selection passes after that addition.

Parent owns independent Astra review and broad final verification for scanner
infrastructure. No full Python suite, PostgreSQL suite, or signed operator
clearance is claimed here.
