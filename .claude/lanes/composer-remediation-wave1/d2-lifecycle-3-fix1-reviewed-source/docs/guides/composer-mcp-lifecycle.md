# Composer MCP process lifecycle

`elspeth-composer` retains its stdio protocol and `--scratch-dir` / `--data-dir`
arguments. In its fresh console interpreter, the command executes the dedicated
standard-library supervisor file, which
owns exactly one private runner. The runner inherits the original stdin,
stdout, stderr, working directory and ordinary environment. The supervisor
neither parses nor relays MCP messages and writes diagnostics only to stderr.

Startup requires binding an ephemeral IPv4 socket on `127.0.0.1`. A fresh nonce
authenticates the bounded private control connection; the runner consumes its
bootstrap environment before importing the application or providers. Loopback
denial fails startup. This private socket does not serve tools or HTTP. Same-user
processes able to inspect or manipulate another process's environment are
outside the nonce's isolation claim.

Before serving tools, the runner registers its real process recovery owner and
executor events. On EOF or catchable shutdown it arms independent supervision,
seals and joins its retained preflight coordinator, joins worker execution and
quarantine observers, and acknowledges completion only after settlement. The
supervisor retains process custody until actual exit. Unresolved work is forced
to exit after the fixed recovery grace; a forced exit is a failure. Embedded
`create_server` / `_build_stdio_server` construction uses the embedding host's
executor owner and does not launch or shut down a process lifecycle.
`main` owns the console process and must not be invoked inside an existing host.

The existing Linux parent-death guard now belongs to the supervisor. Controller
death sends SIGTERM to its stop-intent handler, which arms supervision and asks
the runner to settle cooperatively. Registration detects reparenting against its
recorded parent identity. This extra kernel protection does not run in embedded
hosts. On other platforms, controller loss relies on stdio EOF; the shared
portable supervisor still handles catchable shutdown and runner recovery.
A healthy runner force-exits if its authenticated supervisor channel is lost.
Simultaneously destroying the supervisor and an already unresponsive runner
cannot be made recoverable by this portable user-space mechanism.

The same production supervision path runs on Linux, Windows, macOS and BSD;
platform support has not been narrowed. Linux process/stdio and cleanup controls
have been exercised. Native Windows handle/console and macOS/BSD qualification
remain outstanding; Linux adapter tests do not establish native qualification.
