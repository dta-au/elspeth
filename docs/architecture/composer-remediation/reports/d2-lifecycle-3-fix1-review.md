# Public MCP lifecycle — scoped fix-round-1 rereview

Reviewed 2026-11-09 in
`/workspace/elspeth/.claude/worktrees/composer-remediation-mcp-lifecycle`, at
HEAD `8c35a2e2a9092260f15e88525cbe343df5410d4e`.

**Original P2: ADDRESSED.** The dedicated owner now releases its actual inherited
stdin/stdout descriptors, and the production-path regression demonstrates the
required pipe behavior while that owner remains alive.

**Spec verdict: PASS for this fix round.** The redundant-stdio-copy requirement
that blocked the original review is satisfied within the measured Linux scope.

**Code-quality verdict: PASS for this fix round.** No new actionable breakage was
identified in the three-path fix. These verdicts close the original P2; they do
not repeat or extend the prior full-task review or approve combined release.

## Fix assessment

Read the original finding in `d2-lifecycle-3-review.md`, report section beginning
at line 325, the complete fix-only artifact, relevant production owner/runner
code, new helper and regression, root AGENTS, and CONTRIBUTING. Checkout and
worktree AGENTS/CONTRIBUTING hashes match.

In `portable_supervisor.py:172-182`, successful `_launch_child` precedes closure.
The dedicated branch first closes the Python wrappers (including stdout flush),
then calls `os.close(0)` and `os.close(1)`. This corrects the original
`closefd=False` descriptor-lifetime mistake. Child inheritance has already
occurred, stderr is untouched, and `dedicated_cli=False` does not enter the
closure branch. The fix adds no relay, child, cleanup policy, or provider path.

The helper invokes the actual supervisor script, whose `main()` constructs
`_Supervisor(..., dedicated_cli=True)`. Its real child uses production
`_authenticate_supervisor` and `PortableProcessWatchdog`. The child reads `ping`
and writes `pong` through inherited descriptors before explicitly releasing its
own copies. It then keeps the authenticated control connection/process alive
until the probe writes the release file. The probe checks actual stdout EOF and
`BrokenPipeError` from writing stdin, with owner `poll() is None` before and
after. This observes descriptor ownership rather than wrapper state or eventual
process exit. The child also writes stderr before and after transport closure;
the helper asserts those exact bytes and successful real completion/status 0.

The retained red log and exit 1 show both ownership checks false while echo is
`pong` and owner-alive checks are true. Final retained observations show both
ownership checks true, both owner-alive checks true, echo `pong`, status 0, and
the exact two stderr lines. This is a discriminating regression for the reported
defect, with inherited-stdio and retained-lifetime positive controls.

## Evidence checked

The following are recorded implementation test results, inspected rather than
rerun by this reviewer:

- Bounded cohort: exit 0, **85 passed / 4 deselected**; JUnit has 85 tests and
  zero errors, failures, or skips. Selection explicitly excludes unchanged
  `production_grace` and `blocked_actual_preflight` cases.
- Initial scoped Ruff: exit 1 solely for helper `SIM105`; this is correctly
  retained as a failed static check, not represented as an all-green phase.
- After the helper-only `contextlib.suppress(BrokenPipeError)` cleanup: final
  regression exit 0, **1 passed**, JUnit zero errors/failures/skips, and scoped
  Ruff, format, and whitespace all exit 0.
- Production mypy and contracts checks exit 0. The receipts and individual exit
  files agree. Both exported import roots resolve inside the assigned worktree;
  the recorded interpreter is CPython 3.12.14 on Linux.
- Cohort before/after manifests are equal. Final before/after, handoff, and my
  independently generated live manifest are byte-identical. The only inter-phase
  input change is `tests/helpers/composer_stdio_ownership_probe.py`; production
  and the lifecycle test remained unchanged after the 85-test run.

I independently reconstructed the full live binary patch from tracked and new
files and matched it byte-for-byte to the supplied full artifact. I also
reconstructed the fix delta against the preserved reviewed-source snapshot and
matched the fix artifact. Reverse/whitespace checks for both artifacts exit 0.

| Verified item | Result |
| --- | --- |
| Preserved original artifact SHA-256 | `fa0297dbf92d5faeb6a80099d28b216daa7fb282b1c1eaa25b66e1a19c629949` |
| Fix-only artifact | 7,918 bytes; exactly three paths |
| Fix-only SHA-256 | `0cbb975bcd23d7b74d84e624bdfd23e7e4f7def3d6ba601f4334fab4111aa1bb` |
| Updated full artifact | 87,824 bytes; matches live reconstruction |
| Updated full SHA-256 | `530cd979f8bb0d647623d0390205cb3f12da42aa2df068443c96fb1b426053d9` |
| Live freeze | 6,222 inputs; matches final measured candidate |
| Live freeze SHA-256 | `4c5e043b97da9c0821c125efb68eef781c0e9fdeeaf9e44edbb2f5cf90e70f89` |

Independent raw checks are recorded in
`d2-lifecycle-3-fix1-rereview-verification.json` and
`d2-lifecycle-3-fix1-rereview-freeze.json`. Source evidence is the three-path
`d2-lifecycle-3-fix1.diff`; actual transport observations are under
`d2-lifecycle-3-fix1-final-pytest/test_dedicated_supervisor_rele0/`.

## Limits and separate observations

No out-of-scope finding is raised. Native Windows handle/console qualification
and native macOS/BSD qualification remain outstanding; `os.close` source
reasoning and Linux execution do not establish those platforms. The helper is
a focused real-child ownership control, not a fresh full public SDK acceptance
run; the bounded cohort retains the existing public controls.

No test cohort was rerun, and no implementation edit, commit, installation,
provider call, broad scan, or subagent was performed. The original 600-test and
unchanged long-case evidence are not re-adjudicated here. Parent retains combined
static/boundary/operator-signing, sibling integration, backend/PostgreSQL,
release, and native-platform gates.
