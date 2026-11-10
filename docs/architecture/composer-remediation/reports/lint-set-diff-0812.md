# Keyless trust-tier lint set-diff: 0.8.12 remediation HEAD vs its base

Question: did the 0.8.12 composer remediation add trust-tier lint findings relative to `origin/release/0.8.11-candidate` (a67628c29)? Compared as finding SETS (multisets), not totals. Everything below marked MEASURED comes from the raw files in `.claude/lanes/lint-diff/`; INFERRED is marked as such.

## Commands (MEASURED)

HEAD tree `/home/john/elspeth/.claude/worktrees/composer-0812` (40454302e; `a67628c29` is an ancestor, 75 commits between). Base: `git archive a67628c29 | tar -x` into `.claude/lanes/lint-diff/base/` (deleted after the run). The environment variable `ELSPETH_JUDGE_METADATA_HMAC_KEY` was absent (`grep -c` printed 0). Invocation is exactly the gate's `lints` stage (`scripts/full-suite-gate.sh` line 233), run for both trees with cwd=<tree> and the HEAD interpreter:

```
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
PYTHONPATH=<tree>/src:<tree>/elspeth-lints/src \
  <HEAD>/.venv/bin/python -m elspeth_lints.core.cli check --rules all --root src/elspeth
```

Provenance verified before the run: `elspeth.__file__` and `elspeth_lints.__file__` resolved inside the measured tree for each (base resolved under `.../lint-diff/base/`). The CLI offers `--format json`, but I used the text format because it is what the gate counts; the text lines carry no symbol or fingerprint (only tier_model "Stale ..." entries embed `fp=`).

| tree | exit | findings (gate count `grep -cE '^[^ ].*:[0-9]+:'`) | lines |
|---|---|---|---|
| HEAD 40454302e | 1 | **3604** (reproduces the earlier gate's 3604) | 3604 |
| base a67628c29 | 1 | **3574** | 3574 |

Exit 1 is the by-design keyless result. Raw: `head.txt`, `base.txt`, `head.err`, `base.err`, `setdiff.py`, `report.py`, `setdiff.out`.

## Key and controls (MEASURED)

Key = (rule id, path relative to the scan root with any tree-absolute prefix stripped, message text with ` at <path>:<line>[:col]` offsets and tree prefixes removed). The diagnostic's own line:col is excluded. The message includes the offending source line text, which stands in for an enclosing-symbol qualname (the tool emits none). Comparison is a multiplicity-preserving Counter. Limitation (INFERRED): a pure edit of an offending line's text shows as one removed plus one added.

- Control 1, self-diff: HEAD vs HEAD gives only_a=0, only_b=0.
- Control 2, mutation: HEAD vs a copy with line 1000 deleted gives only_a=1, only_b=0 (detected).
- Zero unparsed lines in either file.

## Result (MEASURED)

Set-diff base to HEAD: **32 new, 2 removed**, net +30 (3574 to 3604). So yes, the remediation added 32 findings. HEAD-only findings are all from 14 files.

New by rule: R6 15, R4 7, R5 6, R9 3, CEC1 1.
New by file: composer_mcp/portable_supervisor.py 6, composer_mcp/portable_bootstrap.py 4, composer_mcp/standalone.py 4, web/async_workers.py 4, web/execution/runtime_preflight.py 4, web/blobs/service.py 2, web/composer/pipeline_planner.py 2, and one each in contracts/process_watchdog_wire.py, web/composer/tool_batch.py, web/paths.py, web/resource_identity.py, web/sessions/routes/composer/proposals.py, web/composer/tools/blobs.py. (Paths relative to `src/elspeth/`.)

Removed (base only): 2
- R5 `isinstance` at web/process_watchdog_codec.py:91 (`if not isinstance(value, dict):`), base line. The codec was refactored (197 lines changed) and the equivalent check now lives in new `contracts/process_watchdog_wire.py:124`, which is one of the 32 new findings. So this is a move, not a new site (INFERRED from the diffstat and the same message text).
- R_TB_SUPPRESSED at web/composer/tools/blobs.py:520, `@trust_boundary` suppression of R5 for `blob_id`. The decorated `_blob_id_uuid_validation_error` was deleted in the diff (MEASURED from `git diff`), replaced by an inline error message.

### New findings under web/composer, web/sessions, web/execution (HEAD line numbers)

web/composer
- web/composer/pipeline_planner.py:446 R6: Exception swallowed without re-raise or explicit error: `except PydanticValidationError:`
- web/composer/pipeline_planner.py:455 R6: same message, `except PydanticValidationError:`
- web/composer/tool_batch.py:1930 R4: Broad exception caught without re-raise: `except BaseException as exc:`
- web/composer/tools/blobs.py:1751 CEC1: `raise ValueError(...)` at web/composer/tools/blobs.py:1751 can escape the tool handler, use ToolArgumentError

web/sessions
- web/sessions/routes/composer/proposals.py:106 R6: Exception swallowed without re-raise or explicit error: `except ProposalStateConflictError:`

web/execution
- web/execution/runtime_preflight.py:173 R4: Broad exception caught without re-raise: `except BaseException as failure:`
- web/execution/runtime_preflight.py:176 R5: isinstance() used: `if isinstance(result, RuntimePreflightFailure):`
- web/execution/runtime_preflight.py:195 R9: dict.pop() with default hides missing keys: `self._retained.pop(retained.task, None)`
- web/execution/runtime_preflight.py:243 R6: Exception swallowed without re-raise or explicit error: `except asyncio.CancelledError as original:`

Other new findings (outside those three trees): composer_mcp/portable_bootstrap.py 90, 98 (R6), 115, 116 (R9); composer_mcp/portable_supervisor.py 200, 210, 228, 236, 277 (R6), 221 (R5); composer_mcp/standalone.py 40, 56, 60 (R4), 54 (R6); contracts/process_watchdog_wire.py:124 (R5); web/async_workers.py 304, 612 (R4), 392, 397 (R6); web/blobs/service.py 2941, 2954 (R5); web/paths.py:33 (R5); web/resource_identity.py:10 (R6). Full text in `setdiff.out`.

I did not judge whether any new finding is policy-wrong; this is a set measurement only.

## Stale-allowlist / signature warnings (MEASURED)

stderr of each run holds 114 "stale allowlist entry allow_hits[N] binds to a deleted source file" warnings (plus 4 shape-only downgrade warnings). After stripping tree paths, the 114 stale warnings are identical in base and HEAD (diff empty); they bind to files from the removed guided mode (e.g. web/composer/guided/*, web/sessions/routes/guided_operations.py). Both trees therefore carry the same pre-existing stale-entry debt. The in-corpus "Stale tier-model allowlist entry" and `allowlist.unused_rule` findings are likewise equal across the trees (they are not in the diff). No signature-drift difference.

## Why the earlier 5,433/5,454 differs from about 3,600

MEASURED from docs: RESUME.md:86 records the gate's lints stage at `dd3b3524c` as `findings=5454`, and the 5,433 baseline is `combined-foundations-lints.json` at `b4064d270` (combined-foundations-lint-review.md: "all rules and `src/elspeth`", integration worktree, both source roots). That is the same rule selection and root as now, so the rule selection is not the explanation. Those counts are on an earlier lineage than the current HEAD (`b4064d270` is the pre-merge integration tip; the merge later brought 33 more candidate commits, per loose-ends-sweep.md:93). INFERRED, not measured: the drop to about 3,600 on both trees (the base a67628c29 itself scans to 3574) points to the candidate lineage having removed roughly 1,800 findings, probably the guided-mode removal; I did not rerun `dd3b3524c` or `b4064d270` to confirm. The 5,4xx-era numbers are therefore not comparable to these, and the in-lineage comparison is base 3574 vs HEAD 3604 above.
