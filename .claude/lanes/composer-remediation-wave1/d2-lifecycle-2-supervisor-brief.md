# adjacent02.17 slice 2 — private portable supervisor and control endpoint

Serial implementation brief, prepared 2026-11-09; no implementation performed
by this brief. Start only after parent accepts slice 1 and provides current
worktree/base. Read local `AGENTS.md`, `CONTRIBUTING.md` whole-tree gates,
canonical `d2-portable-lifecycle-design.md` and `d2-lifecycle-1-report.md`.
Portable support and the private authenticated loopback choice are authorized.
No agents/commits/pushes/providers/migrations/broad suites; parent owns broad
checks. Do not expose a partially integrated owner through the public CLI.

## Outcome and boundary

Build the actual lightweight supervisor, private protocol and child-side owned
watchdog endpoint, exercised by isolated real subprocesses. Keep the public
`elspeth-composer` entry and ordinary `run_server` behavior unchanged until the
following integration slice. New private modules may be callable by production
integration and dedicated test runners; an unsupported ad-hoc invocation must
fail closed before worker admission. No HTTP, tool listener, Web app topology,
D2 path-authority changes or replacement executor.

Place MCP-only supervisor/control modules under `src/elspeth/composer_mcp/`.
Reuse existing closed codec/value types where sound. If adding a
SHUTDOWN_REQUEST frame, update all closed-schema/direction tests and prove the
Linux Web helper still rejects messages it does not support. Do not weaken
existing frame validation to accept a generic payload dictionary.

## Implement the design's ownership contracts

* Parent launches one actual runner child with `Popen`, retains its identity and
  process object, and exclusively owns poll/signal/reap. Target authority never
  comes from a received PID. On POSIX reset inherited SIGCHLD auto-reap before
  spawn; no other thread/handler/library reaps. Never signal after observing
  exit. Windows force termination uses the retained process handle via Popen.
* Parent binds exactly IPv4 `127.0.0.1:0` and holds the listener before spawn.
  Issue one fresh 256-bit nonce through child bootstrap environment; never log
  it or put it in argv/tool/provider context. Child consumes/removes bootstrap
  variables before runtime imports or subprocesses. Close listener after the
  one authenticated handshake. Document local bind as a runtime requirement.
* All socket I/O is nonblocking and bounded. Fixed startup/exchange budgets,
  bounded peers/frames/buffers and a selector loop must let deadline and actual
  child-exit observation proceed during partial frames, floods or bad peers.
  A failed handshake/owner construction never silently enables workers.
* Preserve first immutable recovery receipt and deadline; repeated requests,
  invalid frames or stalled sends cannot extend it. Arm on recovery, channel
  failure and parent shutdown; graceful intent is separate from forced kill.
  At expiration kill only the still-owned child and reap it. Actual termination
  failure is a failure, never a completion witness.
* Child endpoint implements slice 1's semantic owner methods. Graceful request
  drains/cancels its explicitly bound root serving task once; forced request
  terminates the current child with nonzero status. Independent parent still
  enforces expiry if the event loop is stuck. Use real `ProcessRecovery`, not
  a log-only or fixture callback.
* Use a single child-side reader and one retained exchange response; handle
  supervisor-to-child shutdown intent without competing readers or orphaning
  late acknowledgements under repeated cancellation. Parent signal handlers
  record intent only. Windows graceful shutdown uses the channel, not
  `Popen.terminate` (which is forced there).
* Completion validates the exact nominal witness/identity. Parent retains
  custody through final child exit and propagates real status. Bound the
  post-completion exit wait without extending an already armed deadline.
  Bootstrap completion is allowed only before side effects. Close owned
  sockets/handles on every branch; no clean status for an unresolved child.

## Required production-driven test matrix

| Behavior | Real positive and negative controls |
|---|---|
| Supervised target | Launch real minimal private runner through production supervisor; handshake authenticates actual Popen child; forged reflected PID never changes target |
| Authentication | Valid child succeeds; wrong/missing nonce, extra peer, partial prefix, excess frames and wrong direction fail closed; known-positive parser controls prevent vacuous rejection tests |
| Deadlines | First BEGIN arms once; replay/partial-frame flood cannot extend; fixed production-budget recovery child really expires; shorter injected unit budgets cover ordering without public knobs |
| Process custody | Real POSIX child hang is killed/reaped; inherited SIGCHLD=SIG_IGN does not lose custody; no kill after observed/reaped exit; status accurately preserves normal and forced outcomes |
| Independent recovery | Real child stops its event loop or blocks worker/cleanup; supervisor still kills by deadline; does not depend on child asyncio progress or shared pool |
| Graceful and forced | Parent catchable stop sends shutdown intent/arms deadline; responsive child settles; unresponsive child is forced; Windows adapter tests pin handle operation semantics without claiming native execution |
| Channel failure | Close authenticated channel without COMPLETE and prove bounded failure; kill supervisor with healthy child and prove child monitor fails closed; document simultaneous-supervisor-loss/hung-child limit |
| Completion | Valid witness after settlement; wrong owner/nonce/bootstrap phase refuses; repeated cancellation does not issue premature COMPLETE; post-completion atexit hang does not strand parent |
| Existing Web | Existing real Linux helper/codec/recovery behavior remains unchanged through slice 1's semantic API |

Subprocess probes must call the production supervisor/control endpoint and actual
OS child APIs, not a test-written supervisor or no-op owner. Wrap each destructive
case in an outer process/test timeout; keep pytest itself outside kill authority.
Run Linux real children plus pure protocol/native adapter tests. Do not call
monkeypatched platform selectors native Windows/macOS/BSD qualification. Never
kill arbitrary discovered PIDs or process groups to clean failed tests.

Record behavioral red before filling the relevant implementation, not merely
new-module import failure. Existing public stdio missing-owner reproduction must
remain explicitly outstanding: this private slice is not the public defect fix.
Use focused tests, scoped Ruff/mypy and applicable changed-contract gates only;
lane-private logs/exit codes, both worktree import roots and serial subprocess
tests. No broad suite or provider call.

## Handoff gate

Write `d2-lifecycle-2-report.md` and `d2-lifecycle-2-review.diff` (new files
included) in canonical lane. Include complete changed-file review, ownership
and termination state transitions, exact commands/exits, real child evidence,
bootstrap/stdio inheritance assumptions, Web parity and honest platform limits.
Public entry remains unchanged. Parent reviews private owner before dispatching
slice 3; any inability to uphold exact target custody or deadline independence
is a blocker to integration, not a reason to wire a weaker fallback.
