# Portable public MCP lifecycle slice 3 — independent review

Reviewed 2026-11-09 against the exact dirty candidate at
`8c35a2e2a9092260f15e88525cbe343df5410d4e` in
`/workspace/elspeth/.claude/worktrees/composer-remediation-mcp-lifecycle`.

**Spec verdict: changes requested.** The real public owner, preflight/executor
settlement, accepted implementation rulings and public acceptance evidence are
substantially satisfied, but the explicit redundant-stdio-copy closure
requirement is not implemented by the chosen calls.

**Code-quality verdict: changes requested, one P2 finding.** The lifecycle and
retained recovery ownership changes are coherent and appropriately narrow.
Correct the descriptor-lifetime mistake below and pin the actual pipe behavior
before accepting this slice. No additional blocking finding was identified in
the reviewed task scope. This is not a native-platform or combined-release
approval.

## Finding

### P2 — Close the supervisor's actual inherited stdio descriptors

Location: `src/elspeth/composer_mcp/portable_supervisor.py:176-177`,
`_Supervisor._run_owned` dedicated CLI branch.

`sys.stdin.close()` and `sys.stdout.close()` close Python's wrappers, but the
original CPython standard streams use `FileIO(..., closefd=False)`. The
underlying descriptors 0 and 1 therefore remain open in the supervisor. This
leaves the supervisor holding the client's input read end and MCP output write
end throughout its lifetime. If the runner closes its transport while the
supervisor remains alive enforcing cleanup/exit deadlines, the client cannot
observe output EOF or the loss of the input reader solely from runner closure.
The owner independently keeps the streams alive, contrary to the canonical
design and the implementation report's explicit claim.

This was independently measured in the worktree's CPython 3.12.14 using two
bounded stdlib-only subprocesses with actual `PIPE` stdin/stdout. With the exact
two production wrapper-close operations, `os.fstat(0)` and `os.fstat(1)` both
still succeed and the parent observes no stdout EOF while that process remains
alive. The positive control additionally closes the underlying descriptors;
both descriptors are then absent and the parent observes immediate stdout EOF
while the process is still alive. Both diagnostic children exit 0. No production
or test code was changed and no existing test cohort was repeated.

Evidence, including complete diagnostic child source and interpreter version:
`d2-lifecycle-3-stdio-close-review.json`.

Requested change: after successful child inheritance, close the actual redundant
descriptor/handle ownership in the dedicated process, with appropriate ordering
for Python wrapper cleanup; keep stderr and the runner's inherited copies usable.
Add a subprocess regression that observes actual pipe EOF/reader ownership while
the supervisor is still alive, rather than checking wrapper `.closed` or final
process disappearance. Exercise the production dedicated closure path and keep
a real runner inheritance/stdio positive control. Report native Windows handle
qualification separately. The existing help/public EOF tests all wait for
process exit, which closes these descriptors anyway and therefore cannot catch
this defect.

## Scope and independent artifact verification

Read the integration brief first, then the exact canonical
`d2-portable-lifecycle-design.md`, root `AGENTS.md`, CONTRIBUTING whole-tree gates,
the implementation report, complete 16-file diff, the changed lifecycle modules,
and relevant bootstrap/control/recovery/coordinator/Web caller seams. Reviewed
the public SDK helper, fault helpers, new lifecycle/settlement tests and fixture
changes. No broad branch review, implementation edit, commit, installation,
provider call, subagent, broad scan or test-suite rerun was performed.

The independent verification reconstructed the full binary diff from tracked
changes plus every untracked file and compared its bytes to the supplied
artifact. It also independently ran the canonical symlink-safe input freeze and
compared the resulting manifest to the reviewed one.

| Check | Independently observed result |
| --- | --- |
| HEAD | `8c35a2e2a9092260f15e88525cbe343df5410d4e` |
| Changed-file membership | Exactly 16 paths: eight modified and eight new |
| Complete artifact | 81,379 bytes; exact match to live reconstructed diff |
| Artifact SHA-256 | `fa0297dbf92d5faeb6a80099d28b216daa7fb282b1c1eaa25b66e1a19c629949` |
| Reverse apply check / whitespace | Both exit 0 |
| Live freeze | 6,221 inputs; exact manifest equality with supplement/review |
| Live freeze SHA-256 | `3decb176b74092fbbb291e9a0ce855ad91928a88698015a6cc039491362e167f` |
| 600-test candidate before/after | Identical manifests |
| Subsequent source delta | Only `tests/helpers/composer_stdio_probe.py` and `tests/unit/composer_mcp/test_standalone_lifecycle.py`; production unchanged |
| Supplement before/after | Identical manifests |

Raw independent receipts are in
`d2-lifecycle-3-independent-review-verification.json` and
`d2-lifecycle-3-independent-review-freeze.json`.

The existing JUnit files report 600 tests and two supplement tests, respectively,
with zero failures/errors/skips. The corresponding receipt arrays and available
individual exit files agree on exit 0, including scoped Ruff/format/mypy and
contracts. These are verified recorded implementation results, not tests rerun
by this reviewer. The original public red log/exit retains the actual
`Tool error: AuditIntegrityError` transport result and exit 1; the observer
settlement omission control also retains exit 1. Import provenance identifies
both assigned worktree source roots.

## Requirements and accepted rulings

| Requirement or ruling | Review assessment |
| --- | --- |
| Actual public command and lightweight owner | `composer_mcp.main` runs the dedicated supervisor via `runpy` in the original fresh console interpreter; one retained `Popen` launches the private runner with inherited stdio/cwd/environment. No relay or shell. The actual installed-entry audit test pins original PID, one spawn and forbidden pre-spawn imports. Descriptor closure has the finding above. |
| Canonical stdlib wire preload | Runner loads the actual `contracts/process_watchdog_wire.py` under its canonical name; it does not fabricate parent packages. Bootstrap consumes both environment values before runtime/provider imports. Fresh-child tests pin canonical class/module identity and a real descendant's bootstrap-free environment. |
| Real owner before admission | Standalone binds the root, starts real `ProcessRecovery`, registers real callback/events before catalog construction and serving, and retains its exact coordinator through both transport seams. No placeholder or private-global authorization inference was introduced. |
| Standalone refusal / host preservation | Explicit standalone registration refuses an existing owner/executor/custodian. The host registration API itself is unchanged. Embedded constructors retain the supplied owner/coordinator and never enter the dedicated supervisor or install process-global guards. Real fresh owned/missing-owner tests exercise the public registered handler. |
| Old-generation settlement expansion | Outstanding recovery generations are retained across executor replacement. Successful observers release ownership only after actual physical drain-thread exit and outcome retrieval. Failures/escalations remain for shutdown. Shutdown joins retained observers and threads, preserving caller cancellations and failures. Submission/witness/quarantine/replacement policy and host registration semantics are unchanged. The queue-then-raise and omission-red controls directly exercise this gap. |
| Ordered shutdown / truthful completion | Serving unwinds before teardown; recovery sets draining and the retained acknowledgement is joined before coordinator/worker joins. Each cleanup action is retained under repeated cancellation, later actions continue after a failure, and completion is withheld on cleanup failure. Successful completion follows actual coordinator/worker/observer settlement and owner exchange/monitor joins. Primary failures/cancellations remain distinct outcomes. |
| Linux parent-death guard | Guard runs only for dedicated entry, after intent handlers and before spawn; uses `CDLL(None)` without helper discovery; compares recorded parent identity after registration; exposes syscall failure. Embedded APIs do not install it. Actual controller-loss and channel-loss subprocess controls establish Linux behavior. Non-Linux continues through the shared owner without this syscall. |
| Stdio/status / failure branches | Existing evidence covers public help 0, parse errors 2, clean public EOF 0, startup/transport failure 1, cleanup/channel-loss forced exit 70, and production-grace deadline kill/reap -9. Cancellation and failures are not reported as successful completion. |
| D2, validation and private/public projections | Actual SDK tests use a legitimate null/CSV positive, contained CSV singular/plural forms, real named plugin/graph/schema checks, representative patch, immediate absolute/traversal/symlink refusal with saved private bytes unchanged, legacy named `path_allowlist` failure, and source/sink/provider export redaction with private retention. No provider preview/request is used for the provider export control. |
| Web separation/regression | Web continues to call its original registration API and lifecycle shutdown. The only shared production expansion is recovery settlement. Recorded focused coverage includes existing pidfd/recovery, executor, preflight and path controls. No Web lifecycle, HTTP, session-custody or D2 authority rewrite appears in this diff. |
| Documentation/platform posture | New operational guide documents topology, loopback requirement, embedded APIs and Linux extra guard, and explicitly leaves native Windows/macOS/BSD qualification outstanding. No adapter assertion is promoted to native evidence. |

## Evidence quality and remaining gates

The public probe observes the SDK-created process instead of replacing its owner
or injecting the pytest callback into the child. Fault probes wrap real lifecycle
operations and introduce precise blocking/transport/catalog/executor failures;
the registered preview handler, actual worker admission and actual supervisor
remain in the exercised path. The timeout supplement observes the unchanged
five-second preview timeout before two cancelled callers, then checks one real
admission remains during shutdown. The no-release case records a 29.858-second
remaining wait and a real production deadline kill; the release and quarantine
cases distinguish successful settlement from an escalated generation. These
controls support the reported lifecycle outcome but do not test descriptor
closure independently of process exit.

The complete-artifact/freeze checks and the test-only supplement support use of
the original 600-test evidence without repeating that cohort. After the finding
is repaired, review the changed closure and its new behavioral control, record a
new candidate freeze and run the affected bounded checks. Parent retains combined
all-rule/boundary/static adjudication/signing, sibling integration, appropriate
backend/PostgreSQL release gates and native platform qualification. None is
claimed passed by this review.
