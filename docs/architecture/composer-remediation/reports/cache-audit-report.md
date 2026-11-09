# Cache-hit delivered-envelope audit remediation — tracker 11.15

Implementation frozen for parent review on 2026-11-09. No commit, push, tracker
closure, Page mutation, environment install, signature/suppression update, or
source binding update was performed.

## Candidate

- Worktree: `/workspace/elspeth/.claude/worktrees/composer-remediation-cache-audit`
- Base/unchanged HEAD: `f9c23c59c63bbd3099029e995a778bade594f6c8`
- Complete review patch: [cache-audit-review.diff](cache-audit-review.diff)
- Patch SHA-256: `e13501b914043dd7dd177b32deb5fcdc031503fc2aba366032b8ede6b9e05024`
- Evidence directory: [cache-audit-evidence](cache-audit-evidence/)

Only these three tracked files changed:

1. `src/elspeth/web/composer/tool_batch.py`
2. `tests/unit/web/composer/test_service.py`
3. `tests/unit/web/composer/test_compose_loop_audit_wiring.py`

## Behavior and scope

The cache-hit success audit now serializes the same owned admitted/normalized
result with its established `to_dict()` path that produces provider content.
It no longer constructs the separate `{success,data,cache_hit}` surrogate.
Current validation, version, affected nodes, and any other legitimate envelope
fields therefore survive into the invocation audit and durable private evidence.
The legacy result branch also uses its existing owned `to_dict()` serializer.
Projection remains inside the existing cache failure-audit boundary.

Cache provenance remains in `ComposerToolInvocation.cache_hit`; new response
payloads contain exactly the delivered envelope without the old duplicate
audit-only flag. Historical slim rows are accepted by the existing redaction
projection and conversation outcome reader, covered by a new characterization
control. No historical records or readers were changed.

No cache key, admission/readmission authority, handler dispatch, budget,
progress, provider routing, prompt-guard, redaction, persistence writer, proposal,
or lifecycle policy changed. No fallback masks cached response faults.

## Live registry evidence

`registry.log` and `registry.exit` record imports from both worktree roots and
the live `_REGISTERED_TOOLS` declaration tuple, with selected and non-selected
controls (`get_expression_grammar` and noncacheable `set_source`). Raw result:

```text
list_sources selected
get_plugin_schema selected
get_expression_grammar selected
explain_validation_error selected
get_plugin_assistance selected
get_audit_info selected
list_models selected
list_transforms selected
list_sinks selected
cacheable selected=9, legacy=0
exit=0
```

No fake public tool was introduced to reach the currently unreachable legacy
cache branch. Runtime coverage claims apply to the selected contracts that are
actually cacheable today.

## Red → green and checks

All pytest commands used one `-n 0` process, an explicit worktree cwd,
`PYTHONPATH=$PWD/src:$PWD/elspeth-lints/src`, matching
`-o pythonpath="$PWD/src $PWD/elspeth-lints/src"`, and project-local
`--basetemp="$PWD/.claude/lanes/cache-audit/<run>-tmp"`. Logs were redirected
directly to files, and process exits were saved separately.

| Receipt | Result |
| --- | --- |
| `red.log`, `red.exit` | **7 intended failures**, exit 1, against unchanged production; three current-state replay cases and four Pydantic cache-hit cases reject the slim audit payload |
| `green.log`, `green.exit` | **26 passed**, exit 0; cache suite, catalog miss/hit audit suite, nominal schema prompt-guard producer |
| `final-tests.log`, `final-tests.exit` | **237 passed**, exit 0; complete focused selection below |
| `ruff.log`, `ruff.exit` | All checks passed, exit 0, all three changed files |
| `ruff-format.log`, `ruff-format.exit` | Three files already formatted, exit 0 |
| `mypy.log`, `mypy.exit` | Success, no issues in the one changed source file, exit 0; ordinary unused-override notes only |
| `contracts.log`, `contracts.exit` | Contract alignment and soft-mapping census pass, exit 0; raw census: 2615 soft occurrences / 416 files / 60 boundary-parsed |
| `frozen-gates.log`, `frozen-gates.exit` | **5 passed**, exit 0, attribute-contract gate under the frozen tracked tree |
| `diff-check.log`, `diff-check.exit` | `git diff --check`, exit 0 |

The red command selected:

```text
tests/unit/web/composer/test_service.py::TestDiscoveryCache::test_cache_hit_rebuilds_result_envelope_from_current_state
tests/unit/web/composer/test_compose_loop_audit_wiring.py::TestComposerDiscoveryAuditPreservesResult::test_cache_hit_audit_preserves_pydantic_payload
```

The final focused pytest command selected:

```text
tests/unit/web/composer/test_service.py::TestDiscoveryCache
tests/unit/web/composer/test_compose_loop_audit_wiring.py
tests/unit/web/composer/test_prompt_guard_producer_parts.py::test_real_freeform_batch_and_cache_retain_complete_nominal_schema
tests/unit/web/composer/test_discovery_response_contracts.py
tests/unit/web/sessions/routes/test_tool_call_outcomes.py
tests/unit/web/composer/test_redact_tool_call_response.py
tests/unit/web/test_sessions_composer_attribute_contracts.py
```

The non-pytest checks ran `.venv/bin/python -m ruff check <three changed files>`,
`.venv/bin/python -m ruff format --check <three changed files>`,
`.venv/bin/python -m mypy src/elspeth/web/composer/tool_batch.py`, and
`.venv/bin/python -m scripts.check_contracts`.

The new real producer checks drive discovery → real `set_source` mutation →
cache hit. They compare exact miss/hit provider envelopes with invocation audit
JSON and durable private evidence, including its retained SHA-256. They verify
version 1 → 2, current validation, affected nodes, unchanged discovery data,
two discovery invocation rows with miss/hit flags, and one discovery handler
dispatch (plus the real mutation). Four actual catalog producers independently
verify Pydantic payloads, exact delivered envelope equality, and one handler
dispatch across two audited calls.

Existing negative controls remain green for corrupt owned cached values,
different bound contracts, and encoder failure: exactly SUCCESS then PLUGIN_CRASH
in the recorder, no malformed hit provider egress, one real handler dispatch.
The corrupted-cache cases additionally verify the durable tool rows contain
only the original miss, with no duplicate/new successful hit persistence.
The existing nominal-schema prompt-guard test proves cache reuse retains its
producer-owned reference handling.

## Freeze and limitations

`final-freeze.sha256` records:

```text
09b8788b990592142790604255fa4ba1926a5c47ae2dfb01b052191dffd0679e  src/elspeth/web/composer/tool_batch.py
b6a78ae07b8fae60098f96a6097e56ae4c546458337e0864c9eadab8ad541243  tests/unit/web/composer/test_service.py
6de566a36946306dcf8efa7b4303852f67279662f94a2db6e58a1cf7cbda5426  tests/unit/web/composer/test_compose_loop_audit_wiring.py
```

`gate-tree-before.sha256` / `gate-tree-after.sha256` cover the entire tracked
tree, hashing symlink targets as text. Their byte comparison exited 0, and the
three final source/test checksum verifications all returned OK (`freeze.exit`).
An initial attempt to feed tracked directory symlinks directly to `sha256sum`
exited 123 before running the contract gate; the corrected symlink-aware
measurement above completed successfully. No candidate source/test was edited
after the final focused run began.

No failures remain in the checks run. Per the parent scope, no broad/full suite,
PostgreSQL suite, all-rule lint corpus, installation, or independent child review
was run. The parent still owns Astra review, final combined gates (including
ordinary full-suite/PostgreSQL and applicable source-binding checks), and
integration. This report does not claim those gates or tracker closure.
