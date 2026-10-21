# Protected operator runtime source

This directory prepares the separately installed signer runtime. It does not
install it, create OS principals, change access settings or activate signing.
The supported source profile is Ubuntu on x86-64 Linux; other or unqualified
layouts refuse. Real signing remains disabled until operator qualification.

`launcher.c` builds a statically linked, non-setuid executable for the fixed
`/usr/local/libexec/elspeth-operator` entrypoint. The default build has no admitted
operator/candidate identities and refuses. A separately qualified build binds
distinct principals and protected artifact paths. The launcher checks startup
ownership, modes and resolved OS paths before executing the fixed loader and
interpreter with an isolated import path, no site processing and a closed
environment. The loader cache is disabled and the selected library root is
protected. Exact interpreter, stdlib, native dependency and loader closure still
requires qualification of the actual installed build.

Selected signing/verification/override values and explicitly named old/new rekey
variables travel in a sealed private descriptor. The protected bootstrap checks
the exact artifact manifest before admitting those values. Names such as
`LD_PRELOAD` remain data until startup authentication completes. Other ambient
variables are not restored; dotenv interpolation cannot rely on unrelated shell
variables. Existing selected values take precedence over the signer's env file.
UTF-8 values and caller-relative paths are preserved. Candidate source, bundles
and allowlists are data, never import roots. There is no generic signing RPC.

`package_runtime.py` assembles supplied wheels and platform libraries offline,
without importing wheel code. It rejects editable startup files, collisions,
symlinks and traversal, and inventories the resulting artifact. Run its `--help`
for source packaging arguments. This prepares bytes for review; it does not
authenticate a supplied wheel, install a dependency closure or qualify an SDK.

The installed runtime also needs a private `/run/elspeth/operator` directory
with protected ancestors and an operator-owned mode-0700 leaf. Git observation
uses that directory rather than ambient `TMPDIR`. It copies bounded regular Git
metadata into a config-free view, uses only raw object/index/ref operations and
reads working bytes through no-follow descriptors. Includes, transformations,
unsupported index/object layouts and exceeded resource bounds refuse explicitly.
The view permits at most 1 GiB and 100,000 copied entries; ordinary and linked
worktrees are supported. Copying a large object store has a measurable cost.

Provider imports and CLI execution use the independent supervisor described in
[`../judge-runtime/ubuntu/README.md`](../judge-runtime/ubuntu/README.md).
Provider credentials belong to its separately provisioned worker homes. The
signer never starts a judge child or forwards its environment. All transport
observations return through bounded typed RPC, while the signer retains policy,
timestamps, signature verification, journals, rollback and publication.

Editable CLI and key-reader guards refuse accidental custody but cannot undo
earlier imports in an editable process. Use only the independently authenticated
entrypoint for signing, rekey and authoritative verification. Missing protected
startup or worker admission has no editable/in-process fallback. The legacy
baseline generator remains disabled.

Source tests use synthetic keys, controlled metadata and fake providers. They
do not qualify installed ownership, genuine admitted startup, filesystem/process
confinement, service-manager behavior, provider login/cloud routes or real keys.
