# Ubuntu judge source profile

This directory is reviewable source. Nothing here installs a service, builds an
image, creates accounts, changes access, supplies credentials or activates a
provider. The client refuses when independently provisioned enforcement is
unavailable. No real-key, provider, cloud SDK or live systemd qualification is
established by the unit tests.

An operator must authenticate and install the supervisor service independently
of every signer invocation. Never start it from a keyed Python process. The
fixed image `/opt/elspeth/judge-supervisor` contains an ordinary Ubuntu Python
installation, ELSPETH's installed judge modules, systemd clients, their complete
dependency closure and `runtime-manifest.json`. No editable installs, symlinks,
unlisted files or candidate-writable ancestors are admitted by the artifact
validator. The supplied service runs that image as its RootDirectory, so loader,
stdlib, site configuration and application imports resolve inside the same
authenticated filesystem. The socket client authenticates root peer credentials
at the protected endpoint after validating the protected image and fixed
installed service unit. Root is trusted outside the candidate/worker threat
boundary: root can already replace the unit, manifest or keys. Where kernel
process links are observable, the client also checks actual process
root/executable and exact command line. A Python interpreter hash or a worker
identity reply alone does not grant admission.

The externally protected `/etc/elspeth/judge-workers.json` has exactly these
fields: `version` (1), `profile` (`ubuntu-systemd-v1`), `socket`
(`/run/elspeth-judge/supervisor.sock`), `runtime_root`
(`/opt/elspeth/judge-supervisor`), `artifact_id` (manifest SHA256), `executable`
(`/opt/elspeth/judge-supervisor/usr/bin/python3`), `executable_sha256`,
`unit_sha256` (the fixed installed
`/etc/systemd/system/elspeth-judge-supervisor.service` SHA256),
`allowed_client_uids` (unique numeric caller UIDs), and `worker_artifacts` (a
manifest SHA256 for each of `openrouter`, `claude_agent_sdk`, `codex_cli`). The
manifest schema and full inventory are the installed operator runtime's shared
artifact contract. Socket parent and configuration ancestry must be root-owned,
symlink-free and not writable by group/other. Socket write permission merely
permits connecting; authenticated caller UID controls requests.

`/etc/elspeth/judge-supervisor.json` has exactly `version` (1), `profile`
(`ubuntu-systemd-v1`) and `workers`. Each of the same three transport names maps
to exactly `uid`, `gid`, `root_directory`, `artifact_id`, `provider_home`, and
`authentication`. Worker UIDs are unique, nonroot and different from every
allowed caller. Image paths are fixed to `/opt/elspeth/judge-workers/<transport>`
and provider homes to `/var/lib/elspeth-judge/providers/<transport>`. Every worker
image has its own complete protected manifest and `/usr/bin/python3` plus the
installed provider SDK/CLI/helper closure. No image contains signer data or
signer keys. Unsupported/missing images and mismatched identities refuse.

The independently provisioned provider home belongs to that worker UID, mode
0700. Its `provider-environment.json` is a bounded JSON string mapping, owned by
root or the worker, mode 0600. `authentication` selects `subscription`, `api`,
`bedrock`, `vertex`, or `azure`; actual credentials and cloud configuration must
be separately qualified in the corresponding image. Provider environment
names are limited to provider namespaces, and values originate solely in that
private home. HOME, Codex/Claude homes and temporary paths are fixed inside the
worker image. Neither supervisor nor client reads or copies signer environment,
dotenv files, arbitrary rekey values or privileged descriptors.

The operator must prepare root-owned `/run/elspeth-judge` and its `evidence`
subdirectory before starting the service. Only the listed fixed system-manager,
configuration, image, provider-home and runtime mounts enter the supervisor.
Workers receive a fresh system-managed service with a distinct UID, immutable
RootDirectory, private temporary/runtime filesystems and provider home. Read-only
calls receive a root-owned immutable copy of admitted tracked evidence; blinded
calls receive no evidence mount. Candidate roots, allowlists, journals and signer
files are never mounted into workers. Source/tests/documentation path names in
the request are logical paths only. Excluded private, binary or secret-bearing
files are explicitly listed; this is not a complete-codebase coverage claim.
The request retains each selected logical root, including a nested allowlist.
Evidence limits are 8 MiB per file, 128 MiB aggregate and 256 MiB per JSON frame;
larger inputs refuse. Capturing and scrubbing the current repository can take
over a minute before the provider call. Qualify that cost for the installed
artifact and workload rather than inferring latency from fake-peer tests.

Each service has finite runtime, bounded input/output, whole-cgroup kill on
cancellation and an independently reaped launcher. Terminal acceptance requires
service-manager inactive/not-found state, no main process and an empty/absent
kernel cgroup under the trusted computed unit name. Missing cgroup-v2 visibility
refuses. Provider logs and raw exceptions never enter responses. A valid worker
observation still requires signer-owned verdict validation and source/selection
reverification before publication.

Host qualification must verify mount behavior, installed manifest closure,
distinct-principal memory/filesystem access, cgroup-v2 death/reaping, startup and
cancellation deadlines, private provider logins, and actual SDK/cloud behavior.
Nonroot callers normally cannot inspect root peers through `/proc`; they use
root-principal/protected-endpoint authentication without requiring ptrace
capability. Observable mismatches still refuse. This choice relies on externally
trusted root provisioning and does not accept a supervisor identity claim. The
root service can control the system service manager and must therefore
remain separately trusted, key-free and inaccessible to candidate mutation.
