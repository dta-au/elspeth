F2+F3 (U-19) implemented, uncommitted for the reviewer on `codex/0.8.10-exceptions`, HEAD `963b426cfd0688216c7113fed54b439b1e41dc14`.

Files under `src/elspeth/web/frontend/src/`:

- `api/composerOperationObserver.ts`: nominal observation faults, exact non-enumerable cause, separate stream fallback boundaries, typed relationship disposition.
- `stores/sessionStore.ts`: pending recovery presentation, inactive diagnostics, current principal/auth/session guards.
- `stores/composerObservationDiagnostics.ts`: closed-discriminator-only sink.
- `api/composerOperationObserver.faults.test.ts` and `stores/sessionStore.observationFaults.test.ts`: regression and preservation coverage through the real observer/decoders/store.

Test IDs: `R F2 callback fault ownership`; `R F3 bound relationship ownership`; `R real observer-to-store disposition`; `R inactive-session diagnostic owner`; `P transport and decoding recovery`; `P stale fault disposition`. Existing observer recovery/deadline/Stop/restoration and session-store cases also passed.

Observed failing-first assertion text (before the corresponding fix):

- `expected { message: { …(14) }, …(2) } to be an instance of ComposerObservationFault`
- `relationship fault must reject before any backoff: expected false to be true`
- `expected null to be 'Composer updates stopped before the o…'`
- `expected [] to deeply equal [ [ 'snapshot_kind_mismatch' ] ]`
- Principal race: `expected "warn" to not be called at all, but actually been called 1 times`.

Mutation results: M01 callback swallowed; M02–M10 acknowledgement/poll/cancel recovery, removed checks, and plain errors; M11–M13 removed store branch, plain callback error, inactive discard; M14–M15 promoted stream/GET errors; M16 restored POST; M17–M18 generation guards removed; M19 retargeted Stop; M20 foreground principal guard removed. All 20 variants went red and were reverted. M18 initially survived because navigation detached the observer; a post-rejection race test then killed it.

Final validation: 8 focused Vitest files, 319 tests passed, exit 0, one worker; app TypeScript, affected ESLint, and `git diff --check` passed. No full frontend suite, backend changes, uv, or commits. Pending custody and optimistic rows remain pending; causes stay private; only explicit reconciliation restarts observation.

Evidence: sibling `observer-red.log`, `store-red.log`, `principal-race-red.log`, `M*.log`/`.exit`, and `final-focused`, `final-typecheck`, `final-eslint` logs/exit files.
