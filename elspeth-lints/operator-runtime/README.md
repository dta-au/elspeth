# Protected operator runtime source

This directory prepares the separately installed signer runtime. It does not
install it, create OS principals, change access settings or activate signing.
The supported source profile is Ubuntu on x86-64 Linux; other or unqualified
layouts refuse. Real signing remains disabled until operator qualification.
Separate native builds select CPython 3.12 or 3.13 with `make PYTHON_MINOR=12`
or `13`. The sealed handoff binds that compile-time choice to actual interpreter
version and fixed paths. Baseline generation requires the 3.13 build. Rebuild
launcher, bootstrap and admission modules together; old handoff formats refuse.

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

`build_release.py` prepares a complete offline release from an independently
digest-bound catalog and locally supplied native launcher, signer image, judge
supervisor, three judge worker images, baseline supervisor and baseline worker.
Every image needs a complete manifest and ordinary symlink-free archive; flatten
platform links in a separately reviewed preparation environment. The builder
does not acquire artifacts, resolve packages or execute supplied code. It checks
declared ELF dependencies and records exact host interpreter/stdlib/loader/Git
prerequisites. Static checks cannot establish dynamic-import or native closure,
SDK behavior or target-host compatibility.

The key-free `install_release.py verify` and `stage` commands require an
independently supplied release SHA-256; `stage --root` writes only a fresh fixture
tree. Admissions and service units use `.pending` destinations, so checksum
verification does not activate a profile. The future root `install` implementation
also requires existing explicit numeric principals and protected matching host
prerequisites. It refuses existing destinations and never downloads, provisions
accounts, replaces host `/usr`, starts/reloads/enables services or enrolls provider
credentials. Installation, directory ownership changes, activation and actual
artifact qualification have not been exercised by this source work.

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
startup or worker admission has no editable/in-process fallback. The editable
baseline script refuses and prints the protected replacement command:

```sh
/usr/local/libexec/elspeth-operator regenerate-fingerprint-baseline \
  --repo-root /path/to/protected-checkout --env-file /path/to/operator.env
```

The environment file must be outside the checkout. Required signature
verification and expected raw capture use authenticated installed code and a
private full snapshot. Installed first-party rule/resource and application
Python bytes must match that snapshot; rebuild and independently qualify an
artifact when those bytes change. Candidate capture/tests run in the separate
[maintenance worker](../baseline-runtime/ubuntu/README.md). NOTE observations
remain in raw capture without turning a valid enforce gate red. Skip/xfail
cannot substitute for a passing confirmation or signed verification.

Publication additionally requires an externally protected checkout: root or
operator ownership, no candidate-writable input/path ancestry/Git metadata,
and exclusive operator writer custody. The command refuses an ordinary mutable
candidate checkout; it never changes ownership or permissions to manufacture
that condition. A private per-checkout lock serializes baseline operations.
Every byte, tracked census, mode, missing path, HEAD and index binding is
rechecked before the atomic baseline-only replacement. The original fixture
permissions are preserved. Review and commit afterward in a key-free shell;
`--commit` explicitly refuses before loading an environment file or changing
the baseline. The command never executes Git mutation or hooks under keys.

Source tests use synthetic keys, controlled metadata and fake providers. They
do not qualify installed ownership, genuine admitted startup, filesystem/process
confinement, service-manager behavior, provider login/cloud routes or real keys.
