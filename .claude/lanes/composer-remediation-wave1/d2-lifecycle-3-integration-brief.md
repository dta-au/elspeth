# adjacent02.17 slice 3 — standalone integration and public acceptance

Serial implementation brief, prepared 2026-11-09; no implementation performed
by this brief. Start after parent accepts slices 1 and 2 and supplies current
worktree/base. Read local `AGENTS.md`, `CONTRIBUTING.md` whole-tree gates,
canonical portable design and preceding reports. D2 path-admission changes must
also be accepted before claiming combined file-authoring acceptance. No agents,
commits/pushes/providers/migrations/broad suites; parent owns final broad gates.

## Outcome

Wire the fully reviewed portable owner into the actual public CLI and standalone
lifecycle, then prove fresh stdio preview works without pytest's injected
recovery callback. Preserve Web tenancy, embedded host ownership, strict real
preflight, public path redaction and D2 immediate refusal. There must be no
public interval in the final diff where standalone execution submits work with
a placeholder owner. No HTTP, provider behavior, process-global reset or Web
lifespan rewrite.

Primary seams: `composer_mcp/__init__.py` / actual console entry path,
`composer_mcp/server.py` (`main`, `run_server`, `_build_stdio_server`,
`create_server`), private runner/lifespan from slice 2 and appropriate tests.
Preserve public entry name and arguments; do not change packaging unnecessarily.
Inspect final current call graph before wiring it, including prior D2 authority
and cache changes.

## Integrate

1. Public launcher loads lightweight supervision and starts exactly one private
   runner with inherited original stdin/stdout/stderr, cwd and non-bootstrap
   environment. Child authenticates/consumes bootstrap before loading runtime
   or providers. No supervisor output on MCP stdout; no shell invocation,
   stdio relay threads or accidental recursive launch. Keep --help and parse
   errors usable and exit status truthful. Remove/update the obsolete orphan
   workaround only as necessary to match actual parent topology; document why.
2. Standalone child creates real recovery/event ownership before first shared
   submission, then builds server and starts transport. Do not overwrite an
   existing live owner. Embedded `create_server` and `_build_stdio_server` stay
   free of process-global lifecycle setup and use their host's supplied owner.
   Make standalone versus embedded ownership explicit in APIs/tests; do not
   infer authorization from private executor globals or reset them for pytest.
3. Explicitly retain the preflight coordinator passed to the server. On normal
   EOF, cancellation, transport failure and partial startup: stop new tools,
   arm independent shutdown/drain, seal/join actual preflight tasks, join actual
   executor work via `shutdown_async_workers`, settle generation recovery
   observers and owner exchanges, then issue completion only for genuinely
   settled resources. Preserve primary exceptions/cancellations plus distinct
   cleanup failures. Continue later cleanup actions after caught cancellation.
4. If existing executor shutdown lacks a necessary observer-join/ownership seam,
   add only the narrow evidenced lifecycle settlement needed; report it clearly
   for parent review. Do not alter submission/witness/quarantine recovery
   algorithms or abandon required work to make shutdown fast. A withheld
   completion plus actual supervisor termination is the correct unresolved
   outcome, not a successful timeout.
5. Preserve configuration/runtime path identity, strict plugin/graph validation,
   collision and credential controls, existing session audit behavior and public
   export projection. Keep D2 root authority distinct from Web session custody.
   Include concise operational documentation for portable supervisor topology,
   loopback requirement and supported/actually tested platform status.

## Public red-green and acceptance

First rerun the lane's original *actual public executable* reproduction in a
fresh subprocess through `mcp.client.stdio.stdio_client` / `ClientSession`, with
real catalog and legitimate null source/CSV output. Before integration it must
still identify the absent-owner preview failure at the current relevant base
(or document the actual changed baseline). Do not monkeypatch error framing or
inject pytest recovery into the child to manufacture either red or green.

After integration run the following against the real public command and real
production supervisor, without provider network calls:

| Acceptance | Required evidence |
|---|---|
| Original defect | Initialize, author non-file source and valid sink, preview through actual bounded worker succeeds past initialization with no absent-callback transport error |
| Combined D2 positive | Author contained real CSV source and sink, preview reaches real plugin/graph checks with useful verdict; both singular/plural pipeline forms and representative patch succeed |
| Combined D2 refusal | Outside absolute/traversal/symlink path is refused immediately with whole private state unchanged; legacy escaped state independently fails named `path_allowlist` check |
| Public/private projections | Export strips source/sink/provider paths; private live state retains correct options; no secret/bootstrap nonce appears in stdout, tool output, logs or child-derived provider environment |
| Normal completion | EOF exits public launcher/runner cleanly after real coordinator/worker settlement; no lingering owned processes/sockets/tasks; return code is observed, not inferred from a response |
| Outstanding work | Caller timeout and repeated cancellation retain real admission; shutdown during blocked worker cannot claim complete early; release permits clean joins, unresolved work triggers real deadline kill/reap |
| Failure branches | Partial startup, transport failure, worker quarantine/recovery expiry, cleanup failure, parent catchable shutdown and authenticated-channel loss keep truthful failure/status and no premature witness |
| Host separation | Embedded MCP construction/calls with a real existing host owner neither start supervisor nor reconfigure/shut down Web-owned executor/coordinator; missing host lifecycle remains an owned failure |
| Web regression | Existing nominal recovery/pidfd tests and affected path/preflight tests preserve tenant A positives and tenant B/missing-session/root escapes refused |

Use targeted fault fixtures only to trigger precise failure conditions; do not
replace the lifecycle, worker or public registered handler under acceptance.
Destructive/hung-work cases run in separate children under external timeouts.
Observe actual process exit/reaping and worker/task settlement; mocks asserting
`close()` was called cannot certify cleanup. Keep exact original failure and
clean-positive controls so unrelated invalid pipeline configuration cannot
satisfy the preview or path oracle.

Linux runs establish the shared portable production path and Linux OS behavior.
Run native Windows/macOS/BSD jobs only when available and report each actual
result. Otherwise state native qualification outstanding explicitly; no
`sys.platform` patch constitutes native evidence and no inert fallback is
permitted. Preserve current platform posture without claiming unsupported
verification. Parent decides final release qualification from that evidence.

## Focused checks and final handoff

Run focused public subprocess tests, MCP server/audit/session tests affected by
the entry changes, coordinator/recovery/watchdog tests and D2 path/runtime
controls. Apply relevant whole-tree contract gates for changed interface/schema
sites and scoped Ruff/mypy. Use current-worktree import provenance, lane-private
logs and explicit exit codes; serialize process/fault tests. Do not run a full
suite, PostgreSQL suite, provider suite or broad baseline repair from this lane.

Write `d2-lifecycle-3-report.md` and `d2-lifecycle-3-review.diff` including new
files. Include public original red versus final green commands/exits, actual
process topology/custody evidence, changed APIs/configuration requirements,
complete changed-file review, combined D2 results, native-platform limits and
remaining parent gates. Do not claim full D2+02.17 completion if real public
file preview, legacy path refusal, actual cleanup or any designed production
platform branch is still missing. Freeze for parent review; parent owns final
integration, broad testing, tracker/publication decisions.
