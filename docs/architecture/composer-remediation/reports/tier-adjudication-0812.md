# Trust-tier adjudication of the 32 new keyless findings (0.8.12 remediation)

Input: `lint-set-diff-0812.md` (base a67628c29 vs HEAD 40454302e: 32 new, 2 removed). Method: read every site and its data flow. Classes: (a) genuine defect, fixed; (b) honest Tier-3 boundary; (c) moved, not new; (d) rule misfires on correct code. All measurements use the gate's `lints` invocation (cwd=worktree, `PYTHONPATH=<wt>/src:<wt>/elspeth-lints/src`, shape-only verify mode, `--rules all --root src/elspeth`); `elspeth.__file__` and `elspeth_lints.__file__` resolved inside the worktree; `ELSPETH_JUDGE_METADATA_HMAC_KEY` absent.

## Decorator finding (read this first)

`BoundaryRule = Literal["R1", "R5"]` (src/elspeth/contracts/trust_boundary.py): `@trust_boundary` / `@observation_boundary` can suppress only R1 and R5. R4, R6, R9 and CEC1 (26 of the 32) cannot be described by the mechanism at all. Of the 6 R5 sites, one is a real Tier-3 parse (decode_frame) but the decorator is not applicable there (measured, below); the other five are `isinstance` over types ELSPETH owns, which ADR-032 says to type nominally, so decorating them would be dishonest.

## The 32

| # | Site (HEAD lines) | Rule | Class | Reasoning | Action |
|---|---|---|---|---|---|
| 1 | web/composer/pipeline_planner.py:446 | R6 | b | `except PydanticValidationError: return ()` on LLM tool-call arguments when deriving resource keys; the real validation later rejects the call. Third sibling of an existing pattern. | none; operator list |
| 2 | web/composer/pipeline_planner.py:455 | R6 | b | same | none; operator list |
| 3 | web/composer/tool_batch.py:1930 | R4 | d | `except BaseException as exc` stores the exception and re-raises it from inside `dispatch_with_audit` (comment at the site); nothing is swallowed. | none; operator list |
| 4 | web/composer/tools/blobs.py:1751 | CEC1 | a | `raise ValueError` for a tool name the caller (tool_batch) already restricts to update/delete is an owned-invariant breach, not tool feedback; sibling invariants in the function use `AuditIntegrityError`. | fixed (9d128ae6a), test added |
| 5 | web/sessions/routes/composer/proposals.py:106 | R6 | d | Catches only `ProposalStateConflictError` and returns `False` (competing terminal transition); the comment states a missing row escapes. Explicit control flow, not a swallow. | none |
| 6 | web/execution/runtime_preflight.py:173 | R4 | d | `BaseException` from `task.result()` is retained in `retained.failure` and later re-raised or reported. | none |
| 7 | web/execution/runtime_preflight.py:176 | R5 | d | Discriminates our own `RuntimePreflightEntry \| RuntimePreflightFailure` union; nominal check on an owned type (ADR-032). Not external data, so no decorator. | none |
| 8 | web/execution/runtime_preflight.py:195 | R9 | a | `pop(task, None)` directly under `task in self._retained`; the default could only mask a double release. | fixed (99c689ff0): `del` |
| 9 | web/execution/runtime_preflight.py:243 | R6 | d | `CancelledError` is retained (`cancellation`) and re-raised after the join settles. | none |
| 10 | composer_mcp/portable_bootstrap.py:90 | R6 | d | `BlockingIOError` on a non-blocking socket send: normal EAGAIN, not data handling. | none |
| 11 | composer_mcp/portable_bootstrap.py:98 | R6 | d | same, on recv (`continue`). | none |
| 12 | composer_mcp/portable_bootstrap.py:115 | R9 | b | `os.environ.pop(PORT_ENV, None)`: the environment is external input; absence is handled explicitly on the next lines (refusal). Not decoratable (R9). | none; operator list |
| 13 | composer_mcp/portable_bootstrap.py:116 | R9 | b | same for NONCE_ENV. | none; operator list |
| 14 | composer_mcp/portable_supervisor.py:200 | R6 | d | `(OSError, WatchdogProtocolError)` runs `fail_channel()`; explicit handling. | none |
| 15 | composer_mcp/portable_supervisor.py:210 | R6 | d | non-blocking `accept()` EAGAIN. | none |
| 16 | composer_mcp/portable_supervisor.py:221 | R5 | d | `assert isinstance(peer, socket.socket)` narrows `selectors` key.fileobj (stdlib `FileDescriptorLike`); a mypy narrowing of a socket we registered. | none |
| 17 | composer_mcp/portable_supervisor.py:228 | R6 | d | non-blocking `send` EAGAIN (`sent = 0`). | none |
| 18 | composer_mcp/portable_supervisor.py:236 | R6 | d | non-blocking `recv` EAGAIN. | none |
| 19 | composer_mcp/portable_supervisor.py:277 | R6 | d | peer error closes the peer / fails the control channel; explicit handling. | none |
| 20 | composer_mcp/standalone.py:40 | R4 | d | `BaseException` appended to `originals`, aggregated and re-raised after cleanup. | none |
| 21 | composer_mcp/standalone.py:54 | R6 | d | cancellation recorded in `originals`, re-raised later. | none |
| 22 | composer_mcp/standalone.py:56 | R4 | d | `except BaseException: break` hands the outcome to `task.result()` on the next statement. | none |
| 23 | composer_mcp/standalone.py:60 | R4 | d | failure recorded (`cleanup_failed`, `originals`). | none |
| 24 | contracts/process_watchdog_wire.py:124 | R5 | c | The same `isinstance(value, dict)` on decoded frame bytes moved from web/process_watchdog_codec.py:91 (that R5 is one of the 2 removed). It is a true Tier-3 parse, but the module is stdlib-only by contract: importing `elspeth.contracts.trust_boundary` pulls `elspeth.contracts` and failed 12 of the composer_mcp lifecycle/supervisor tests (`assert "elspeth.contracts" not in sys.modules`). Decorator tried and reverted. | none; test added for the refusal; operator list |
| 25 | web/async_workers.py:304 | R4 | d | `BaseException` stored on the reservation / retained as a physical failure, surfaced by the finalizer. | none |
| 26 | web/async_workers.py:392 | R6 | d | `_retain_cancellation(...)`; re-raised via `_raise_lifecycle_originals`. | none |
| 27 | web/async_workers.py:397 | R6 | d | same. | none |
| 28 | web/async_workers.py:612 | R4 | d | `except BaseException: break` then `observer.result()` reports it. | none |
| 29 | web/blobs/service.py:2941 | R5 | d | Discriminates `BlobDeletionPlan \| BlobAtomicDeletionObligation`, both owned classes. | none |
| 30 | web/blobs/service.py:2954 | R5 | d | same union, second arm of the same function. | none |
| 31 | web/paths.py:33 | R5 | d | Deliberate nominal guard on `McpConfiguredRootAuthority`; I removed it as a redundant check and the reviewed tests `test_mcp_root_is_nominal_and_cannot_mix_with_web_custody` and `test_empty_validation_cannot_hide_an_invalid_owned_path_context[wrong-type]` failed. That is the contract (ADR-032). Reverted. | none |
| 32 | web/resource_identity.py:10 | R6 | b | `except ValueError: return None` is the UUID parse of a model-supplied selector; callers refuse on None. Not decoratable (R6). | none; operator list |

Removed (base only): R5 web/process_watchdog_codec.py:91 (moved, row 24) and R_TB_SUPPRESSED web/composer/tools/blobs.py:520 (the decorated validator was deleted upstream).

## Before / after set-diff (same key as lint-set-diff-0812.md)

| | new vs base | removed |
|---|---|---|
| before (HEAD 40454302e, from the earlier report) | 32 (3604 total) | 2 |
| after (this branch, `.claude/lanes/tier/after.txt`) | 30 (3602 total) | 2 |

After by rule: R6 15, R4 7, R5 6, R9 2. CEC1 and one R9 are gone. (A raw compare also shows 3 `allowlist.unused_rule` pairs; they differ only by the scan-root prefix in the message and vanish with the correct per-tree prefix. Control: that comparison with the right prefixes gives 30/2.) The base output reused from `composer-0812/.claude/lanes/lint-diff/base.txt`.

## For operator adjudication / signing

None of these were allowlisted or signed by me. Code is left in its clearest correct form.

Real Tier-3 boundaries (class b/c), need allowlist entries because the decorator cannot express them:
- web/composer/pipeline_planner.py:446 and :455 (R6, LLM argument validation returns no resource key)
- web/resource_identity.py:10 (R6, model UUID selector parse)
- composer_mcp/portable_bootstrap.py:115 and :116 (R9, environment pop with explicit absence refusal)
- contracts/process_watchdog_wire.py:124 (R5, peer frame bytes; moved from the codec; decorator unusable under the stdlib-only constraint, so allowlist, or relax that constraint for `trust_boundary` deliberately)

Policy-wrong or non-defensive (class d), operator may adjudicate or ignore per policy; for the R4/R6 cancellation and non-blocking-socket sites the rule has no notion of "retained and re-raised":
- R4: tool_batch.py:1930; runtime_preflight.py:173; standalone.py:40,56,60; async_workers.py:304,612
- R6: proposals.py:106; runtime_preflight.py:243; portable_bootstrap.py:90,98; portable_supervisor.py:200,210,228,236,277; standalone.py:54; async_workers.py:392,397
- R5 (owned nominal types / stdlib narrowing): runtime_preflight.py:176; blob service.py:2941,2954; paths.py:33; portable_supervisor.py:221

## Checks run

- Focused: tests/unit/web/blobs/test_mutation_admission.py (new test), tests/unit/contracts/test_process_watchdog_wire.py (new), tests/unit/web/execution + tests/unit/composer_mcp at -n 4, tests/unit/architecture/test_session_db_mutation_authority.py (298 passed, 1 xfailed, file untouched), tests/unit/web/composer at -n 4 (13533 passed, exit 0).
- One load-dependent failure, `test_portable_supervisor.py::test_authentication_and_partial_startup_fail_closed[peer-flood]`, failed at -n 4 and passed with `-n 0`; it exercises code this branch did not touch.
- ruff check, ruff format --check, mypy on the touched production files, and `scripts.check_contracts` (soft-mapping census unchanged) pass.
- Limits: no full suite, no testcontainer run.
