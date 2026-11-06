# Ubuntu Python 3.13 maintenance source profile

This is noninstalled source. No image build, account creation, download, service
activation, OS access change, signing key or provider is involved in its source
tests. An absent independently protected profile refuses. Installation and host
qualification require separate operator authorization.

The root supervisor is installed and launched independently before the signer.
It must never descend from a keyed signer process. Its immutable complete image
is `/opt/elspeth/baseline-supervisor`, including CPython 3.13, installed ELSPETH
worker/RPC code, systemd clients, loader/stdlib/dependency closure and
`runtime-manifest.json`. The service uses that image as RootDirectory. The
separate immutable worker image `/opt/elspeth/baseline-worker` contains CPython
3.13, pytest and the dependencies needed by mutable repository capture/tests.
Both images use the installed operator artifact manifest contract. No editable
install, unlisted file, writable ancestor or image symlink is admitted.

The fixed root-owned `/etc/elspeth/baseline-workers.json` has exactly these
fields: `version` (1), `profile` (`ubuntu-systemd-python313-v1`), `artifact_id`
(supervisor manifest SHA256), `interpreter_sha256` (the supervisor image's
`usr/bin/python3.13`), `unit_sha256` (the fixed installed
`/etc/systemd/system/elspeth-baseline-supervisor.service`),
`allowed_client_uids` (unique numeric UIDs), `worker_uid`, `worker_gid`, and
`worker_artifact_id`. The worker UID is nonroot and differs from every caller.
The operator prepares root-owned protected `/run/elspeth-baseline` and its
`snapshots` subdirectory. No provider credential/home/config is mounted.

Client admission checks the protected configuration, installed fixed unit,
complete image/interpreter and root-owned socket in its protected parent.
SO_PEERCRED authenticates root. Root independently provisions these artifacts
and is trusted outside the candidate/worker threat boundary; it could already
replace keys, manifests or units. Observable kernel executable/root/argv
mismatches refuse. A Linux PermissionError hiding root-peer `/proc` data does
not grant worker-claimed identity or require ptrace privilege for admitted
nonroot callers. No response asserts signed validity.

The request contains only the complete frozen tracked input census, its hash
and an independently derived expected baseline. Exact file fields are `path`,
`mode`, `sha256`, `content`; content is canonical base64 of raw bytes. Paths are
sorted, unique, relative and traversal-free. `.git` and `.venv` are reserved.
Regular modes 100644/100755 and safe internal symlinks 120000 are supported;
escaping, unavailable or cyclic symlinks refuse. File/aggregate/count/frame
bounds reuse the judge limits (8 MiB raw file, 128 MiB raw aggregate, 12,000
files, 256 MiB frame); base64 strings have their corresponding finite bound.
The digest is SHA256 of canonical JSON of the full file array. No input is
redacted or discovered by mutable worker code.

The supervisor validates all raw input bindings before materializing a
root-owned read-only private repository. Old baseline fixture bytes remain in
the frozen request; only that private copy receives the expected baseline.
It adds `.venv/bin/python` pointing solely to `/usr/bin/python3.13` in the
trusted worker image. No real `.git`, candidate checkout, active allowlist,
signer directory, key file or provider authentication is mounted.

Only the nonroot worker executes mutable `capture_all` and its confirmation
test, in separate children. Working directory and both candidate Python source
roots are confined to the private copy. The environment is constructed from
constants, with dotenv/plugin autoload disabled and no signer environment or
descriptors copied. The key-free capture's shape-only mode is not a signed gate.
Network is unavailable; runtime, memory, CPU, tasks, temporary filesystem and
output are bounded. Cancellation kills the whole service cgroup, reaps the
launcher and checks inactive/not-found service state plus an empty/absent kernel
cgroup before any terminal response. Raw errors and subprocess logs are not
returned.

Capture must exactly equal the independently expected baseline. Confirmation
uses the exact consistency test under canonical 3.13, `--runxfail`, serial
configuration and a trusted hook that turns direct `pytest.xfail` into failure
(pytest otherwise makes that call a no-op with `--runxfail`). A bounded JUnit
report must contain exactly the named passing test, zero skips/xfails/errors or
failures. These remain external maintenance observations: mutable code may lie.
They never replace the operator's independently derived baseline, required-mode
signed enforce gate, complete input reinspection or atomic publication.

Host qualification remains required for installed closure/startup authenticity,
RootDirectory/mount behavior, distinct-principal file/memory access, cgroup
death/reaping, syscall/startup latency and actual capture/pytest compatibility.
Source tests use small fixtures and fake admissions/services. They do not
establish a live protected maintenance runtime or signed gate.
