# PR276 integration and affected controls

The held streaming branch now uses independently landed PR276 at `d2b73990d137e9725200c897544eebc5b958fdf4`. The prior base was `23822a7477624d6264c60fade0905c19e8c552d7`. This consumes the main dependency without recreating its archive patch. The branch remains uncommitted and unfrozen, with final source handoff NO-GO and John's LOCAL-testing HOLD.

Before incorporation, the complete tracked and nonignored untracked working files were retained with their bytes, modes and hashes. The staged set was empty. The app startup/periodic archive sweep and architecture pin tables were the two overlaps; both were reconciled while the existing source owners were paused. The remaining preserved source files matched their preintegration hashes. Only the isolated branch base/index moved to the landed main commit. The shared checkout remained at `edc844699a350a90a624089e12a5a1e75b7b2dde`, with only untracked BUGREP.md and unchanged SHA-256 `5df889ed9e3e32277b71a0a667e236d5bdc66a2d3c1fbd06b502c125fb14009f`.

The first affected six-module run completed, and its complete log and exit were read:

```text
exit=1
11 failed, 324 passed in 123.42s
```

Ten failures concerned lifecycle controls that still used the previous executor/finalizer/watchdog/error-group interfaces. Their repaired controls retain independent persisted-truth checks after shutdown, actual nominal finalizer admission, the owned test watchdog, both original startup/cleanup exception objects and the factory's precreated drain event. Their focused rerun completed:

```text
exit=0
10 passed, 238 deselected in 7.25s
```

The archive unknown-outcome fixture now matches the candidate’s retained original exception cohort. The independent base/current AST control observes a bare SessionOperationTerminalOutcomeUnknown at HEAD and that same unknown-outcome object plus both original OSError action/reconciliation objects in the working candidate, with identity true for every object; both ordinary-negative cases remain primary-only. Its raw log is storage-archive-base-current-proof.log; completed tool exit 0 is attributed to the storage owner report, and no separate exit file exists. The full archive-recovery fixture module rerun completed 4 passed in 1.29 s, exit 0 (storage-archive-fixture-41.log and .exit, independently read). The initial eleven-failure run is retained; the complete affected six-module rerun and final frozen gates remain owed.

The first affected canonical inventory selection completed with four failures and ten passes. Existing location metadata was corrected only after the live canonical scanner matched every other identity field exactly; writer authority and connection escape policy were retained. The one unmatched trigger-reader fingerprint was reviewed separately: only the PostgreSQL SELECT allowlist adds the two owned async-operation trigger names, and the rest of the function AST matches main. The canonical scanner accepts the unchanged reader and rejects an injected sessions UPDATE. Two preliminary diagnostic assertions used `update` rather than the scanner's actual `raw_update` operation and failed; those failed artifacts remain retained. The corrected control and affected canonical selection completed:

```text
schema reader positive / injected-write negative: exit=0
affected architecture selection: exit=0
14 passed, 259 deselected in 17.98s
```

The two affected PostgreSQL modules ran serially against a private cached PostgreSQL 16 container capped at 256 MiB and 0.5 CPU, with no provisioned external database URL:

```text
exit=0
33 passed in 14.04s
```

Host capacity measurements must use the same escalated process namespace as the tests. Earlier sandbox `/proc` probes could not see three abandoned lane test processes and therefore were not valid host-capacity evidence. Exact argv, worktree, start identity and attached private log paths established their ownership. Their raw unfinished logs were preserved; the exact processes were stopped, and a subsequent host probe measured no active pytest processes. Historical tool interruption code 130 did not establish actual process exit and is superseded by this correction. No previous abandoned run is certified as passed.

The actual provider/revocation and telemetry narrow Daybreak PLAN GOs remain design-level decisions only. Provider/caller custody, source22→26→27 handoff and telemetry now have partial owner implementation and scoped controls; remaining coherent callers, original broader controls, canonical full suites, final frozen independent source reviews and John’s local testing remain owed. The completed telemetry 53-pass caller selection predates its latest physical-thread/constructor/receipt/reuse guards and supplies no fresh whole-source clearance. No merge, deployment, production test, paid-provider test, tutorial bypass, new agent lane or UX work was performed or authorized here. The unrelated archive reviewer payload denial remains unresolved and was not bypassed.
