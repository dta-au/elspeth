# Portable lifecycle slice 2 review

Reviewed 2026-11-09. **Spec verdict: changes requested before integration. Code-quality verdict: changes requested for one concrete protocol handoff defect (P2).** The private supervisor otherwise implements substantive process custody and bounded recovery; the recorded tests provide useful behavioral evidence. This is not acceptance of the public stdio fix, which remains slice 3.

## Scope and immutable candidate

Read the slice-2 supervisor brief, portable lifecycle design, slice-1 and slice-2 reports, local `AGENTS.md`, relevant `CONTRIBUTING.md` whole-tree gates, every changed/new Python file, the census relocation, and the unchanged recovery contract consumed by the new endpoint.

Candidate: `/workspace/elspeth/.claude/worktrees/composer-remediation-portable-supervisor`, HEAD `b4064d2700bd4c9413a1cdcb794a95226681d373`, with the reported ten changed paths. Review artifact SHA256 measured as `2b8cec87e4e67c62aeb6963534b8c756e4d8114e401ee83cd48d292e7f9326d8`. `git apply --reverse --check` against that artifact exited 0. Each of its ten current files matched its SHA256 entry in the final frozen manifest.

No production/test source edits, commits, repeated pytest suites, broad scans, installs, provider calls, signing, Pages, or delegated agents. One new read-only socketpair probe exercised a previously uncovered transition; its code and output appear below. This report is the only review deliverable written.

## Finding

### P2 — Preserve control-stream state across the bootstrap-to-async-reader handoff

**Location:** `src/elspeth/composer_mcp/portable_bootstrap.py:39,64–70`; handoff in `AuthenticatedSupervisor.claim():28–33` and `portable_control.py:_read_frames():70–71`; producer ordering in `portable_supervisor.py:161–166,213–223`.

`exchange_sync()` creates a temporary `FrameReader`, reads up to its full capacity, and returns the first reply only when exactly one complete frame was decoded. The authenticated receipt transfers only the socket and target. The eventual asynchronous reader creates a fresh parser. Consequently:

* A valid `WATCHING_ACK` followed by a complete `SHUTDOWN_REQUEST` in the same socket read is rejected as an unexpected bootstrap frame.
* A valid `WATCHING_ACK` followed by only a prefix of `SHUTDOWN_REQUEST` is accepted, but the parser holding that prefix is discarded. The next reader starts in the middle of the frame and treats the remaining valid bytes as a malformed length. If the entire partial prefix is all that has arrived, the shutdown request has also been temporarily lost.

This is reachable with the production parent, without a malicious peer or forged authority. Set parent stop intent before HELLO is processed. The HELLO branch authenticates the child and queues WATCHING_ACK. On the following main-loop iteration, `stop_requested` is still true and the now-authenticated control socket causes SHUTDOWN_REQUEST to be appended before the queued ACK is flushed. Both frames share the same bounded outgoing buffer and can be sent together. A stop arriving around the ACK send can instead produce fragmentation across the handoff.

The result is startup failure or a later monitor-triggered forced exit for a responsive child receiving a legitimate graceful shutdown. Custody and the independent kill deadline remain intact; this is not a foreign-PID or deadline-extension vulnerability. It does violate the private protocol's required graceful shutdown behavior and retained single-reader semantics. The current stop tests first wait for the child's `ready` event, so they cannot observe this window.

**Repair expectation:** retain the decoder and any decoded/pending control frames across bootstrap and ownership transfer, or introduce an equally bounded protocol transition that ensures unsolicited shutdown cannot enter a throwaway reader. Preserve exact direction/identity validation, fixed budgets, and the single consuming reader. Do not assume TCP receive calls correspond to frame boundaries or drop extra bytes. Cover the synchronous bootstrap completion/abort exchanges as well, since they also encounter legitimate shutdown intent before root binding.

**Regression expectations:** ACK-only positive control; ACK plus complete shutdown in one read; ACK plus fragmented shutdown prefix across handoff; real production supervisor with stop intent set before/during HELLO and a responsive child settling; unchanged independent deadline when the child does not settle. Test that root cancellation happens once and that valid traffic is neither discarded nor misclassified. This repair belongs to slice 2, before public integration.

### Exact read-only reproduction

Executed from the candidate worktree with `PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" .venv/bin/python -` (exit 0):

```python
import socket, threading
from elspeth.composer_mcp.portable_bootstrap import exchange_sync
from elspeth.contracts.process_watchdog_wire import (
    FrameKind, FrameReader, WatchdogFrame, encode_frame,
)

def probe(prefix):
    a, b = socket.socketpair()
    a.setblocking(False)
    hello = WatchdogFrame(FrameKind.HELLO, 'a' * 64, 1)
    ack = encode_frame(WatchdogFrame(FrameKind.WATCHING_ACK, 'a' * 64, 1))
    stop = encode_frame(WatchdogFrame(FrameKind.SHUTDOWN_REQUEST, 'a' * 64, 1))
    def peer():
        b.recv(1028)
        b.sendall(ack + stop[:prefix])
    t = threading.Thread(target=peer)
    t.start()
    try:
        reply = exchange_sync(a, hello)
        print('prefix', prefix, 'bootstrap', reply.kind.value)
        if prefix:
            b.sendall(stop[prefix:])
            remaining = a.recv(1028)
            try:
                print('next-reader', FrameReader().feed(remaining))
            except Exception as exc:
                print('next-reader', type(exc).__name__, str(exc))
    except Exception as exc:
        print('prefix', prefix, 'bootstrap', type(exc).__name__, str(exc))
    finally:
        t.join()
        a.close()
        b.close()

probe(0)
probe(2)
probe(1000)
```

Measured output:

```text
prefix 0 bootstrap WATCHING_ACK
prefix 2 bootstrap WATCHING_ACK
next-reader WatchdogProtocolError Watchdog frame length refused
prefix 1000 bootstrap WatchdogProtocolError Unexpected Composer bootstrap frame
```

This probe establishes the parser handoff defect using the production synchronous exchange. It is not represented as a new real-Popen test or native-platform qualification. The production-parent reachability argument above comes directly from its queue and loop ordering.

## Spec assessment beyond the finding

* **Exact process authority:** `_Supervisor` retains the actual Popen; received PID/nonce only validate frames. The same loop polls, kills and waits. It resets inherited SIGCHLD disposition on POSIX before spawn, restores handlers afterward, and checks observed exit before kill. Signal handlers record intent only. Windows force goes through retained `Popen.kill()`; graceful intent uses the control channel. No discovered-PID target, process-group kill, competing reaper, waiter thread, or worker-pool dependency was introduced.
* **Bounded protocol:** listener binding is precisely IPv4 loopback and occurs before spawn; the fresh nonce is environment-only and child bootstrap removes both variables before contract imports. Peers, buffers, frame counts, outgoing bytes, startup/exchange time and selector polling are bounded. Authentication closes the listener and other peers. Failed authentication cannot create an endpoint. The bootstrap stream handoff is the exception identified above.
* **Deadline independence:** `_arm()` retains the first recovery timestamp/deadline. Reads and writes are nonblocking, each selector iteration is bounded, and child status/deadline checks precede socket dispatch. Channel/protocol failure and parent stop arm recovery. Completion takes the minimum of the existing deadline and a two-second exit allowance. Forced termination/reap failure raises, and actual exit remains required even after DISARM_ACK.
* **Child recovery and cancellation:** endpoint binds one root task and cancels it once, retains BEGIN and COMPLETE tasks behind shields, and has one asynchronous reader routing the expected response plus shutdown intent. Real `ProcessRecovery` owns monitoring and required-generation escalation; force is self-only `os._exit(70)`. Repeated caller cancellation does not replace the retained exchange. Production integration must continue to join the retained work before issuing the witness.
* **Completion and status:** exact nominal witness class and exact target equality are required; root binding permanently closes bootstrap completion. Witness construction is the lifecycle's assertion after joins, not resource introspection by the endpoint, as explicitly designed. Parent custody continues after COMPLETE, clean zero status requires completion without failure, nonzero child status is preserved, and the dedicated executable re-reflects POSIX signal termination after reap.
* **Stdio/scope:** Popen inherits stdin/stdout/stderr directly, with no MCP proxy or stdout logging in the owner. Public launcher/server and D2 authority files are absent from this patch. Closing redundant launcher stdio copies remains an explicitly documented next-slice obligation.
* **Existing Web:** accepted codec/value relocation preserves supported Web imports and nominal identity. AST comparison against HEAD found all four moved lifecycle value classes, `WatchdogFrame`, `FrameReader`, encoding, reason enum and validation helpers identical. The wire changes are the closed shutdown enum member and excluding it from completion-only fields. The Linux helper state machine is unchanged; its direct-file import resolves the same stdlib-only contract source. The real unsupported-direction control verifies it still rejects portable shutdown intent.

No second actionable defect was established in this scoped review. The short production supervisor is auditable and its explicit custody rules are materially stronger than a PID-based fallback. The approved single contract definition and compatibility exports avoid duplicated type authorities; the census update is the relocation of the two existing soft mappings, with no new whitelist or suppression.

## Evidence quality and limits

The recorded `focused.exit` and `status-focused.exit` both contain 0. Parsed XML reports **78 tests, zero errors/failures, 111.024s** and **44 tests, zero errors/failures, 30.504s**, respectively. The latter log records one deselected production-grace test. This review did not rerun those cohorts.

Both before/after manifest pairs are byte-identical, with 6211 entries each. Recomputed canonical JSON hashes match the report: prestatus `3665df9ec1c4082e6149a74730852fe38b3e86e908dc012b87817eeecd25742e`, final `a6b5a1cdac84516c22a6c7d2304d80d0ab6a9ea0c974ed915008c66b4f55d327`. Comparing entries shows only `portable_supervisor.py` and its unit-test file changed between cohorts. The final ten candidate paths all match the final manifest. Recorded imports resolve both project and lint packages to the assigned worktree. This is bounded provenance verification, not a rerun of the broad manifest walker.

The full-run XML records the portable real production-grace fault at **30.393s** and the Linux Web armed-channel fault at **30.387s**. The portable fault receipt contains actual returncode `-9`, `reaped=true`, `failed=true`, `completed=false`. The required-worker probe calls the real shared executor, quarantines the real generation, observes `ProcessRecovery.required_generation_expired`, and blocks on real executor cleanup while withholding COMPLETE. Its persisted `required-observed` marker and killed/reaped receipt corroborate that the independent parent, rather than a fixture callback, resolves the hang. The responsive parent-stop receipt records completed/reaped status 0 and both `stopped` and `completed` markers.

The retained-cancellation mutation's exit-1 receipt shows status 70 instead of expected 0; the arm mutation changes the retained receipt and loses the immutable marker; the signal-status mutation catches translation of child SIGTERM into ordinary exit 143. These are behavioral controls rather than missing-import failures. They support the tested properties, while not covering the newly found startup transition. The original startup-clock mistakes are honestly excluded from behavioral-red evidence.

Scoped Ruff, formatting, mypy and contracts receipts all have exit 0 and corresponding successful logs. Mypy covers seven production files, formatting/Ruff nine Python files; contracts reports 2615 soft occurrences in 416 files, 60 boundary-parsed. These receipts are not broad-suite, signed-lint, or native-platform clearance.

The healthy-child supervisor-loss case verifies actual child exit, including Linux zombie exit status when present; it does not prove a destroyed supervisor reaped its child. The post-completion atexit case demonstrates retained parent custody beyond witness acknowledgement. The existing private stdio control proves direct stdout inheritance, not the complete public MCP transport. Adapter tests inspect actual launch options and retained-object operations but do not execute Windows.

## Handoff obligations

Fix the bootstrap stream handoff and add the stated transition controls before accepting this private owner for integration. Then slice 3 must supply the thin public launcher/private authenticated runner, consume bootstrap authority before runtime construction/admission, bind the actual root, start real recovery monitoring, configure required executor ownership exactly once, and perform all coordinator/task/executor/monitor joins before truthful completion. It must close redundant parent stdio copies safely and test actual public MCP arguments, initialize/operations/EOF, stdout cleanliness and exit behavior. The existing public missing-owner reproduction stays OPEN until those checks pass.

Local IPv4 loopback binding remains a documented runtime requirement. Same-user process/environment access is outside nonce authentication's isolation claim. Simultaneous abrupt supervisor destruction and an already-hung child remains the stated portable user-space limit. Native Windows/macOS/BSD lifecycle/console/handle qualification, parent-owned broad gates and signed/static corpus adjudication remain outstanding; none is silently replaced by mocks or this review.
