# .10 security implementation handoff

> **0.8.10 assembly annotation (2026-10-24, unit U-10).** The source is a
> Codex security-lane handoff, carried verbatim from WIP preserve commit
> `255f1cff3` (byte-identical to `f3e6862ed` on
> `codex/security-boundaries-20261020`). The annotations below are the only
> changes. The body is historical context, not authority. The 0.8.10 release
> differs from it as follows:
>
> - **Carried:** `ec6fe2c81 8acf10728 eed99de69 108ad8b01 f16cc411b adb27d441
>   4f7ed7ac6 8ab3fed55 cca549f41` plus `c382f9bb9` (specced mocks), cherry-picked
>   with `-x`. **Dropped by maintainer ruling:** gateway commits `8f5619255` and
>   `d993e6d26`. They require Ubuntu/systemd, host UIDs and `/etc`/`/opt` roots,
>   which cannot run on the AWS ECS or Azure Container Apps targets.
> - **Gateway rows superseded:** the gateway adapter finding
>   `csf_cfe54ceaf27dac2419e05855` is **closed by design**. Adapters are
>   operator-trusted code, like plugins. See `c6a9fea80` (landed on
>   `release/0.8.10-assembly` as `b04ba8532`), `docs/security-assurance/04-threat-model.md`
>   and `gateway/README.md`. Every gateway worker, broker, epoch or Ubuntu-profile
>   statement below describes code that 0.8.10 does not ship.
> - **Signing is suspended** pending the on-prem migration. No sign-bundle,
>   rekey or baseline regeneration runs, and signing is not part of CI. Findings
>   are tracked by the keyless trust-tier set-diff (see `AGENTS.md` § Operator
>   signature verification).
> - **"Approved by the operator" claims are UNCORROBORATED.** No maintainer
>   record supports them. They are marked inline and confer no authority.
> - The signer findings `csf_49e6…`, `csf_652f…` and `csf_e22b…` remain **OPEN**
>   until the protected runtime is installed and qualified (post-release
>   operator work).

All four HIGH findings are must-land .10 scope and remain OPEN. The selected
commits below are partial safeguards plus the accepted exact-stale functional
change. They are not a claim that the four security boundaries are closed.
Real signing remains disabled pending on-prem migration and qualification.

## Exact committed selection for the integration owner

Required integration base: `0b8cfa9e92fa0578372f9699999ad211a4eeb7fb`,
the targeted npm repair whose direct parent is certifi repair
`820aa43c7525a9e1b74cfe1705ef7ceeafc2328a`, in turn based on the originally
qualified .9 input `2e869240a388d1ede0454f1fbb9b019d22a9a9da`. Neither older
input contains the latest npm repair; do not substitute an older base or lock.
Selected committed source tip: `cca549f41283c02a8df245e2ff54c9648b0a73e3`.
This includes protected baseline restoration, offline runtime packaging and
supervised gateway epochs. Installed qualification remains unexecuted.
Security branch: `codex/security-boundaries-20261020`; original ancestor:
`2d5c0557e2d3dfcf6f176b94e9d17290d3da7a31`.

Apply these commits in order in the integration owner's separate index:

```sh
git cherry-pick ec6fe2c81b8e33b714a6504b996210fe6d461cca
git cherry-pick 8acf10728ec08cd886bdf106ee12fa63aac033fb
git cherry-pick eed99de690907a81d41f0462c1aba629fe0204dd
git cherry-pick 108ad8b0133dadc97a4e8f98a0927227769583e9
git cherry-pick f16cc411b2cb10ba578c4b07dd9b6fdd2076f938
git cherry-pick 8f5619255169b0c716749ffca467d3821ce1fcbb
git cherry-pick adb27d4417a8094d5d8b825125382685e4f197b0
git cherry-pick 4f7ed7ac6563629fbf1db67a4396a88c3c29a8d5
git cherry-pick d993e6d26df95c23946da8567fbe9f2296b90a91
git cherry-pick 8ab3fed554222b22d9df70477f5bf4d7295b087a
git cherry-pick cca549f41283c02a8df245e2ff54c9648b0a73e3
```

> **0.8.10 annotation:** `8f5619255` and `d993e6d26` are dropped and not
> carried. `c382f9bb9` is added after `cca549f41`.

| Commit | Result |
| --- | --- |
| `ec6fe2c81` | Removes automatic repository maintenance from signing/rekey completion. |
| `8acf10728` | Packages canonical boundary contracts, removing checkout-relative resource dependence. |
| `eed99de69` | Adds verified exact-stale retirement without judge calls, including rollback preservation and public MCP discovery coverage. |
| `108ad8b01` | Refuses SDK transport before optional import when the signing-key variable is present, including empty. |
| `f16cc411b` | Disables standalone key-bearing baseline maintenance; historical arguments explicitly refuse. |
| `8f5619255` | Moves all gateway adapter execution behind RPC, pins metadata before private input, and requires protected Ubuntu admission. Includes an unactivated, unqualified service template. **0.8.10: dropped, not carried.** |
| `adb27d441` | Adds protected operator startup, callback-free source/audit Git observation and separately supervised judge RPC, with entrypoint guards and compatibility tests. Source only; installation and confinement remain unqualified. |
| `4f7ed7ac6` | Restores baseline generation through protected Python 3.13, externally protected publication custody, trusted signed verification and independent key-free capture/strict confirmation. Supports separately compiled 3.12/3.13 native profiles. The editable wrapper and automatic Git commit still refuse. |
| `d993e6d26` | Adds mandatory gateway v2 root-broker admission, complete artifact/unit pins, stateful epochs and independent worker stop/reap source. Adds offline split-artifact assembly and an inactive installer. Production v1 admission refuses. **0.8.10: dropped, not carried.** |
| `8ab3fed55` | Adds externally digest-bound offline assembly/verification for native, signer, judge and baseline artifacts, declared ELF-edge checks, fixed startup inventories and pending-only installer source. |
| `cca549f41` | Repairs the baseline publication-custody omission for empty source directories. Preserves file-only snapshots and resource limits; adds individually unsafe-directory controls and the final-check insertion regression. |

The security branch merged the actual repair without conflicts or history
rewriting as `07c881a422de9c757d0271a5c3820c48f16bb500`, with parents
`8e23b8a1b387c83b9b5d94a9e0b707441cf591b8` and
`820aa43c7525a9e1b74cfe1705ef7ceeafc2328a`. Both manifests and both locks
are byte-identical to that repair commit. Parsed manifests require
`certifi>=2026.10.19`; both locks resolve `2026.10.19`. Compared with `2e869240`,
certifi is the only package whose locked version changed.

The subsequent npm repair was merged without conflicts as
`cdb0fd519fc25552d43959cd3c39c35fec63bb5b`, with parents
`d565d008bd63189b36c863833653e1dfb32792e4` and
`0b8cfa9e92fa0578372f9699999ad211a4eeb7fb`. Only root `package-lock.json` and
frontend `package.json`/`package-lock.json` changed. All three file SHA-256
digests match the repair's Git blobs exactly. Parsed lock inventories show only
follow-redirects `1.16.0 -> 1.16.1` in root and Vite `6.4.3 -> 6.4.5` in frontend;
the frontend manifest and lock project record both require `^6.4.5`. The prior
four Python dependency files still match the repaired base exactly. Frontend
tracked source also matches `0b8cfa9e`; the security source effects are unchanged.

The former isolated `820aa43c`-plus-six-source-commits tree
`06e5f1554e4b395056fe336ec6c4ad5b09729d62` is **historical**. It does not
describe the new npm dependency base or TC's combined integration tree. Do not
force equality to it, replay older locks, or infer combined release qualification
from the earlier isolated-tree receipt.
The source branch history was preserved by the merge. The frozen donor,
shared checkout and integration owner's real index were not changed. This
handoff document is outside the selected source commits; it can be carried
separately if useful.

## Finding-by-finding remaining work

| Finding | Current source status | Concrete work required for closure |
| --- | --- | --- |
| `csf_49e6c1d4c587149b07c83516` — keyed repository maintenance | Source replaces snapshot and audit Git subprocesses with bounded config-free metadata views and direct no-follow reads. Protected baseline generation is restored in `4f7ed7ac6`; automatic repository callbacks and the editable wrapper remain disabled. | Qualify the installed observer/Git/scratch boundary, externally protected frozen-checkout custody and independently isolated baseline worker. Actual installed signed verification/publication remains unqualified. No caller-supplied success receipt or shape-only substitute. |
| `csf_652f5deeb8d970ec214048eb` — checkout-controlled signer startup | Source candidate provides a static native entrypoint, closed interpreter startup, protected artifact inventory/authentication, sealed selected-value handoff and guards on editable CLI/key readers. Operator renderers name the absolute entrypoint. | Independently install and qualify actual principals, ownership, loader/interpreter/stdlib/native closure and exact artifacts before real key admission. Source tests do not establish genuine installed startup. |
| `csf_e22b5e22120644ec52c10bfd` — Claude SDK in keyed parent | Source candidate routes every production judge call through bounded RPC and an independent root supervisor. Provider imports/CLIs execute only in admitted workers; signer parsing, policy/time, HMAC and publication stay local. | Qualify exact SDK/CLI/dependency artifacts and OS worker enforcement, private authentication, cancellation/death and cloud routes. No provider or live worker execution occurred. |
| `csf_cfe54ceaf27dac2419e05855` — gateway adapter credential/network authority | Mandatory RPC source in `8f5619255` is extended by supervised v2 admission and offline artifacts in `d993e6d26`: no adapter imports in production core/identity, complete inventories/unit pins, pinned metadata, worker invocation and broker epoch, bounded independent stop/reap. | Qualify actual core/broker/worker artifacts and Ubuntu confinement, including denied secret/process/filesystem/IP access, exact mount/inventory views and supervisor kill/reap. No profile is installed. Historical Alpine image/runtime smoke and ACA/AWS profiles remain unqualified. This is not an anonymous authentication bypass. **0.8.10: superseded. Closed by design (`c6a9fea80`/`b04ba8532`); the worker work is not carried.** |

`core/` in the signer rows is `elspeth-lints/src/elspeth_lints/core/`.
IDs retain their original finding spelling; connector lookup failure is not
evidence of closure.

## Concrete recommended boundary and decision

Implement source for an Ubuntu-first protected operator runtime and separated
judge/adapter workers. The installed operator launcher and interpreter tree
must be outside checkout/user-site/PYTHONPATH control, with ownership and the
reviewed artifact authenticated independently before keys are introduced.
The trusted signer may parse candidate bytes; it must not import candidate
modules or run checkout maintenance. Judge workers need their own provider
authentication home/environment, without signer keys, files or process memory.
Use an authenticated local channel with bounded, versioned JSON, never pickle,
Python objects, exception objects or caller-selected executable commands.
OS identity/profile installation and real key activation remain separate
operator actions; source implementation does not authorize either.

> **0.8.10 annotation:** the gateway paragraphs below are superseded by
> `c6a9fea80`/`b04ba8532` (adapters are operator-trusted code, closed by design).

For the gateway, the smallest complete source change preserving arbitrary
installed Python adapters is a mandatory isolated worker plus a core-owned
RPC client. The core keeps inbound bearer authentication, OAuth/token storage,
upstream HTTP and public logging. Worker execution must cover:

- package/entry-point import, transitive startup and constructor;
- `descriptor`, including completion and readiness calls;
- `validate_configuration` and optional `validate_model_target` lookup/call;
- `build_invoke`, `parse_success` and `classify_error`;
- offline adapter identity execution, with admitted artifact identity bound
  independently rather than trusted from the worker's self-report.

The core receives only bounded data and reconstructs its own descriptor,
InvokePlan, canonical response and error-classification types. Retain
send-time origin/path/header validation, response bounds, single 401 replay,
optional-hook absence, safe fixed errors and existing state/sequencing behavior.
Do not send the full gateway config, HTTP handles, tokens, credentials or raw
worker logs. Timeouts, death, malformed replies, incompatible protocol and
missing enforcement fail closed; do not offer a production in-process fallback.

**Gateway source architecture approved by the operator:** mandatory workers and
Ubuntu-first source implementation, accepting that existing derived images must split core
and worker artifacts and be requalified. Public HTTP and adapter method contracts
remain, but current co-installation topology cannot remain secure under this
threat. ACA and future AWS/Fargate remain unqualified until equivalent enforcement
is established. This choice does not authorize creating identities, changing
access/security/network settings, installation, deployment or live signing.

> **0.8.10 annotation:** the operator approval claimed above is UNCORROBORATED,
> and its gateway scope is superseded by `c6a9fea80`/`b04ba8532`.

The reviewed signer/judge protected-runtime source implementation was approved
by the operator on 2026-10-21 at 07:53:36 UTC. That approval covers code and
tests only. Installation, host security changes, provider calls, SDK artifact
installation and real-key activation remain outside this source approval.

> **0.8.10 annotation:** the dated operator approval claimed above is
> UNCORROBORATED. The signer commits ship in 0.8.10 on the coordinator's
> assembly plan, not on this claim.

A same-UID child, environment stripping, SDK pin alone, adapter fingerprint,
readiness assertion or co-located sidecar is insufficient. Removing third-party
adapter support would be a different product decision and is not the proposed fix.

The local shared interpreter reported `claude-agent-sdk: not installed` through
`importlib.metadata`; no SDK installation/import/provider call was attempted.
The source extra is still `claude-agent-sdk>=0.2.87`. An exact qualified artifact
cannot be asserted from that lower bound. Artifact acquisition/qualification in
a separate key-free environment remains an explicit prerequisite.

## Verification and practical critique

Ordinary native commands in the security worktree used both source roots,
disabled dotenv loading, private fixture/cache/log paths and explicit process
exit codes. All tests used fake transports or synthetic keys, not real signing.

| Source unit | Observed evidence |
| --- | --- |
| Exact-stale unit at `eed99de69` | Affected tests: 213 passed; owning-package mypy: 135 files; masquerade gate plus two negative controls: 3 passed. Removed rollback guard produced the four intended failures before restoration. |
| SDK refusal at `108ad8b01` | Four intended pre-fix failures; then SDK transport, tools, shared judge integration, signing bundle and rekey modules: 248 passed, process exit 0. Mypy: 135 files. |
| Maintenance refusal at `f16cc411b` | Six intended pre-fix failures; then script regression plus signing-bundle/rekey modules: 161 passed, process exit 0. Script mypy: one file. |
| Gateway source at `8f5619255` | Unit/conformance selection: 804 passed, exit 0; existing standalone wrapper/SDK-isolation checks: 3 passed, exit 0. Mypy: all seven changed boundary modules passed. Ruff check/format and normal hooks passed. |

Ruff check/format, diff checks and applicable normal commit hooks passed for
these units. The source branch was clean after each commit. Logs and JUnit are
under the lane's `ordinary-*` paths; they are coordination output, not files to
stage. These are overlapping test selections, not an additive test total.
No full Python suite, PostgreSQL suite or TC combined release qualification is
claimed here.

Gateway evidence is `gateway-final.xml/.log`, `gateway-wrapper-final.xml/.log`
and `gateway-mypy-final.log` under the lane. The gateway unit and wrapper
selections overlap. The wrapper also verifies the standalone conformance import
path; nested temporary files were kept inside this worktree.

After merging `820aa43c`, the actual merged security tree at `07c881a42`
passed the gateway's own configuration selection (804 tests, exit 0) and the
root-config affected security/resource/MCP plus standalone-wrapper selection
(330 tests, exit 0). Owning-package mypy passed all 135 lints source files.
Evidence: `dependency-gateway.xml/.log`, `dependency-security.xml/.log` and
`dependency-lints-mypy.log` under the lane.

The first combined invocation used root pytest configuration for both projects
and exited 1 with 127 failures and 125 setup errors, all in gateway tests: the
header recorded `asyncio: mode=Mode.STRICT`. Gateway declares `asyncio_mode =
"auto"` in its own configuration. The two successful selections above used
their respective configurations; the failed invocation is retained as
`dependency-focused.xml/.log`, not counted as passing evidence.

During the `820aa43c` reconciliation, the shared virtualenv was not installed or
synchronized; its measured certifi distribution was `2026.1.4`. These local
functional checks do not qualify execution under the new locked TLS dependency.
The fresh .9 CI build owns that locked-runtime validation; no local audit or CI
completion is inferred here.

For the later npm reconciliation, local checks were limited to Git diff/ancestry,
JSON parsing, exact file digests, manifest/lock consistency and package-version
deltas. No new Python source changed, so the earlier security checks retain their
original source/lock binding rather than being relabelled as a new run. The .9
owner reported isolated npm installs/audits, frontend tests, dependency/type
checks and build for `0b8cfa9e`; those remain attributed owner results. No frontend
test/build was run here against the old shared module symlinks, and no installed
closure was changed. Combined frontend/build qualification must use the patched
actual closure. No PR/CI completion, merge, deployment or HIGH closure is inferred.

The focused candidate peer review found a concrete prompt-to-readiness/log
channel through mutable descriptor versions, an optional noncallable-hook
regression and a lifespan/readiness mismatch. All were addressed. Descriptor and
capability metadata are now pinned once, under locks, before private options,
targets, requests or upstream data are sent. Removing descriptor pinning made
the canary test fail with its private prompt published as the version (one
intended failure); restoration passed the final selection. Noncallable optional
hooks retain absent semantics, and actual lifespan coverage preserves the
incompatible-API readiness response while wrong/unavailable peers refuse startup.
Completed adapter refusals do not poison future valid calls; malformed data,
death, timeout and cancellation invalidate the client without replay.

The source profile is not qualified runtime evidence. No image build, runtime
smoke or hostile adapter on an enforced host profile was executed. Do not reuse
historical Alpine single-container CI acceptance as acceptance for this topology.
The four dependency paths `pyproject.toml`, `uv.lock`, `gateway/pyproject.toml`
and `gateway/uv.lock` were not independently edited. The actual .9 repair was
merged after its owner completed the handoff; all four files match `820aa43c`
exactly. The integration owner must preserve this floor and use that repaired
base rather than copying the older manifests or locks. Root `package-lock.json`
and frontend `package.json`/`package-lock.json` were likewise carried only through
the actual `0b8cfa9e` merge, without independent lock editing or broad upgrades.

## Signer/judge design: practical review feedback

The existing reviewer found the direction sound, with these concrete required
details. The operator subsequently approved implementing the reviewed source
changes; this feedback remains the implementation contract. *(0.8.10: that
approval is UNCORROBORATED; see the annotation at the top.)*

1. Authenticate the actual installed interpreter, signer modules, dependencies,
   startup configuration, resources and writable ancestors independently before
   key admission. A checkout wrapper, self-checked hash, `-I` flag or profile
   marker is insufficient. Operator authorization must bind the operation,
   source/bundle and key references; workers must not gain a generic signing API.
2. Replace every keyed command renderer and alternate entry point together:
   MCP commands (`mcp/server.py:217-224,338-346`), diagnosis repair/migration
   (`core/judge_signature_diagnosis.py:691-697,719-727`), repair dry-run and
   recovery (`core/cli.py:2932-2963,3516-3559`), standalone signing, and any
   check/audit/reaudit holding a verification key. Missing installed runtime
   must refuse; no editable or PATH-selected fallback.
3. Replace bare Git plus inherited environment at `core/source_snapshot.py:283-292`.
   A fixed executable or `--no-ext-diff` alone does not prevent clean/process
   filters or fsmonitor callbacks. Use trusted raw object/index/ref observation
   plus direct candidate bytes, or a trusted helper that cannot execute repository
   callbacks. Preserve linked worktrees, source/HEAD/dirty-state semantics and
   no-mutation refusal; unsupported layouts must explicitly refuse.
4. Move the shared `call_judge` boundary and all callers into the worker client,
   including OpenRouter, Claude SDK, Codex, reaudit, quality checks and MCP preview.
   Do not copy signer environment and strip known names: rekey names are arbitrary.
   Provider profiles/homes belong to independently launched workers; all their
   imports, helpers and native shells must remain outside signer authority.
5. Bind each request/result to the operation, source/request snapshot and intended
   independently supervised peer. Bound JSON and evidence access; no executable
   paths, environment files, HMAC, overrides or asserted source-verification
   success cross the channel. Signer code reconstructs fields, validates finite
   confidence/integer token accounting, sets policy/timestamp/bindings, scrubs
   rationale and owns signatures/publication. Worker isolation does not itself
   prove an honest provider verdict. Specify cancellation/death/restart and
   supervisor enforcement of read-only evidence, process/filesystem/network scope.
6. Preserve UTF-8 key bytes and env precedence, caller-relative paths, arbitrary
   rekey names, dry-run integrity, full-census verification, authenticated journals,
   terminal BLOCK/resume outcomes, partial-rekey semantics and exact-stale rollback
   bytes/modes/path preservation. Preserve subscription/API/Bedrock/Vertex/Azure,
   blinded/read-only modes, model identity, cached-usage `None` versus zero, and
   configuration/auth/billing versus transport/contract failure behavior.

Here `core/` and `mcp/` refer to `elspeth-lints/src/elspeth_lints/`, bound to
the historical review at `f16cc411b`; the approved increment now changes those
signer files. The fresh source investigation and candidate review are retained
under the existing lane. Candidate review found remaining inherited-environment
Git audit helpers and a normal-repository read-only evidence size regression.
Both were reproduced and repaired before the final frozen package run: the
four audit helpers now use the callback-free observer, and evidence limits
support the existing large files while retaining finite aggregate/frame bounds.
The candidate review predates these repairs; it is not a blanket approval of
the subsequent diff. Their regression and compatibility checks are recorded below.
The local SDK is absent. Qualifying an exact SDK/bundled CLI/dependency closure,
installed artifact ownership/authentication and separate-principal enforcement
requires a separately authorized key-free runtime. At this earlier increment,
standalone baseline generation remained disabled. The later protected restoration
and its distinct verification boundary are recorded below.

**Current source authority:** the operator's later dated approval lifts the
earlier review-first source hold. It grants no host/access/security-setting,
installation, key, provider or deployment action. The source increment is
committed with focused tests, one candidate-review cycle and normal local gates.

> **0.8.10 annotation:** the "later dated approval" is UNCORROBORATED.

## Remaining acceptance

> **0.8.10 annotation:** the gateway sentences in this section are superseded
> by `c6a9fea80`/`b04ba8532`; the gateway source is not carried.

The mandatory gateway client/dispatcher/worker, supervised epochs and offline
packaging source are committed and tested with synthetic artifacts and peers.
Actual host confinement, independently qualified core/broker/worker artifacts
and hostile-adapter acceptance remain unexecuted.
Signer/judge source implementation is extended by protected baseline restoration
and offline packaging through `8ab3fed55`; exact SDK
acceptance and real operator activation remain separate requirements.
Integration and broad gates remain with the integration owner. No HIGH is reported fixed and no real
keys/access/settings/deployment are activated here.

## Approved signer/judge source increment

Source and operator workflow details live in
[`elspeth-lints/operator-runtime/README.md`](../../elspeth-lints/operator-runtime/README.md)
and the [Ubuntu judge profile](../../elspeth-lints/judge-runtime/ubuntu/README.md).
The default native build is deliberately unconfigured and refuses. No operator
runtime or supervisor/worker image is installed, and no systemd unit is active.

Compatibility boundaries are explicit: only selected signer variables enter
the protected interpreter after authentication; unrelated ambient variables
are unavailable for dotenv interpolation. Selected-value precedence, UTF-8,
caller-relative paths and arbitrary old/new rekey names remain supported.
Provider credentials must move to independent worker profiles. Key-free preview
also requires the admitted supervisor; it has no local SDK fallback.

Git observation supports ordinary and linked worktrees and preserves whole-source
dirty-state and tracked-input semantics. Config includes, nonempty attributes,
transformations and unsupported object/index layouts refuse rather than execute
callbacks or reinterpret source state. Copies are bounded to 1 GiB/100,000
entries per view. A normal repository snapshot succeeded locally; copying the
object store costs seconds and is reused within each metadata audit operation.

Read-only evidence uses 8 MiB per file, 128 MiB aggregate and 256 MiB framed JSON
bounds. The earlier 1/32/64 MiB limits rejected existing tracked input and were
not retained. A key-free real-repository capture with the enlarged limits
succeeded: 71,947,878 included text bytes and 75,761,852 encoded JSON bytes,
before duplicating the selected nested allowlist capability. This diagnostic
took 70.5 seconds. Private, binary and secret-bearing exclusions are explicit;
the worker does not receive every byte of the checkout. Snapshot capture cost
and the exact installed profile still need operational qualification.

The root integration also closes the source-excerpt check/read symlink race
with descriptor-based no-follow reads and retains the rejudge proposal helper
as key-free. These source changes grant no installation or activation authority.

## Final source verification at `adb27d441`

The source commit is `adb27d4417a8094d5d8b825125382685e4f197b0`, with parent
`a2c9f111980748aba34d0192918df6749afd89d7`. It changes 53 owned source, test and
operating-documentation paths. This optional integration handoff is excluded
from that commit so it can be carried separately. No dependency manifest or
lock was independently changed; all seven dependency files still match
`0b8cfa9e92fa0578372f9699999ad211a4eeb7fb` byte for byte.

The frozen final command selected `tests/unit/elspeth_lints` and
`tests/unit/scripts/test_codex_tier_model_rejudge.py`, using both source roots,
disabled dotenv, private in-repository temporary paths and `-n 0`. It exited 0:
**3,246 passed, one skipped, one expected failure**, in 502.90 seconds. Native
JUnit records 3,248 cases, zero failures/errors and two skipped records, one
representing the expected failure. Before/after hashes of 6,221 tracked and
nonignored candidate paths were unchanged; normal commit hooks also left these
bytes unchanged. Evidence is `final-package.log`, `.xml`, `.exit` and the
before/after JSON under the lane.

The expected failure is the existing fingerprint-baseline consistency test;
it reports baseline drift rather than establishing clearance. The other skip
is the empty-owner case already rejected by argparse. Neither was newly marked
or suppressed. This run covers the lints package's whole-tree masquerade and
file-walker gates, signing/rekey journals, rollback and mocked provider behavior;
it is not the full engine or PostgreSQL suite.

Owning-package mypy exited 0 over 140 source files. Ruff check, format check
(235 files), diff whitespace checks, branch safety and all applicable normal
commit hooks passed, including the keyless trust-tier comparison against HEAD.
Signature verification with the operator key was not performed.

Executed negative controls reproduced candidate Git callback exposure in the
four audit helpers, missing Git artifact admission, oversized ordinary evidence
and the source-excerpt symlink race. Repairs retain refusal before unsafe
execution/read; the final suite exercises their legitimate controls as well.
The actual repository blanket audit succeeded using one private metadata view
(5.96 seconds), and actual repository evidence capture succeeded under the
revised bounds described above. Test admission seams use explicit synthetic
runtime/worker fixtures; they do not prove installed positive admission.

Full security verification remains **blocked** on separately authorized artifact
and host qualification: native loader/interpreter/dependency closure, actual
principals and mounts, worker process/filesystem/network denial and kill/reap,
exact SDK/CLI/provider authentication and cloud routes. The later baseline source
restoration does not supply installed qualification. No SDK was installed, provider called,
real key accessed, service activated or deployment performed. All four original
HIGH findings remain OPEN. The integration owner retains combined release
qualification and merge custody.

## Protected baseline restoration at `4f7ed7ac6`

The protected command is `regenerate-fingerprint-baseline --repo-root …
--env-file …`. It requires the authenticated Python 3.13 profile and a separately
protected frozen checkout whose full input tree, ancestors and Git metadata are
inaccessible to the candidate writer. Ordinary mutable candidate checkouts refuse.
The command never changes ownership or permissions to manufacture admission.
The env file must be outside the candidate checkout; `--commit` refuses before
key loading or baseline mutation. A later key-free manual Git commit is explicit.

The signer authenticates installed first-party application/lint bytes against
the frozen candidate and refuses a mismatch before relying on runtime-backed
catalog rules. It verifies the required signed gate itself, derives the existing
20-rule raw baseline, and sends bounded raw snapshot bytes to a separately
supervised, key-free worker. Candidate capture and the exact consistency test run
there. Strict confirmation requires the named test to pass; skip, xfail, direct
`pytest.xfail`, malformed results and a changed census refuse. Worker observations
cannot confer signed validity. The signer repeats the gate, expected census,
custody and full-input checks before mode-preserving atomic fixture publication.
A private operator lock serializes admitted operations. A post-rename fsync failure
reports unknown publication durability, rather than falsely claiming no mutation.

One candidate review identified NOTE-only gate rejection, a publication race and
an installed-versus-candidate catalog gap. The repairs retain NOTE observations,
require external full-tree custody plus serialization, and require exact installed
first-party source equality; scanner children receive both candidate source roots.
That review predates the repairs and is not a blanket approval of the final diff.

The frozen owning-package selection exited 0 in 474.90 seconds: **3,341 passed,
one skipped, one expected failure**. Native JUnit records 3,343 cases, zero
failures/errors and two skipped records. Before/after hashes of 6,230 tracked and
nonignored paths were unchanged, including after normal hooks. Evidence:
`baseline-package.log`, `.xml`, `.exit`, and before/after JSON under the lane.
The expected failure remains the historical fingerprint-baseline drift; no new
xfail or suppression was introduced. Owning-package mypy passed 144 source files;
Ruff check/format covered 242 files; normal branch safety and commit hooks passed.

A separate key-free compatibility diagnostic froze the actual repository:
6,230 files and 119,114,281 raw bytes. The trusted 20-rule census produced 4,305
findings; real candidate capture agreed exactly, and strict confirmation reported
the single named passing test with zero failures/errors/skips/xfails. Original
baseline bytes were unchanged. Evidence: `baseline-ownrepo-capture3.log` and
`baseline-full-compat.log`. This diagnostic used the local Python 3.13 interpreter
and a private copy, with synthetic admission. It did not execute the actual
RootDirectory/systemd profile, protected installed startup or real signed gate.

All four HIGH findings remain OPEN. No real baseline was regenerated, key used,
artifact acquired, account or access setting changed, service installed/activated,
provider called or deployment performed. Offline packaging source is recorded
below; installed qualification remains separate.

## Offline packaging and supervised gateway source

> **0.8.10 annotation:** `d993e6d26` (the gateway half of this section) is
> dropped and superseded by `c6a9fea80`/`b04ba8532`. `8ab3fed55` is carried.
> Its gateway/OpenRouter worker roles, `/opt`/`/etc` roots and systemd units
> are operator-host packaging with no deployed consumer in 0.8.10.

`d993e6d26df95c23946da8567fbe9f2296b90a91` changes 16 gateway source, test and
operating-documentation paths. Production accepts only the protected v2 profile.
The credential-free root broker is independently launched; core and adapter use
distinct nonroot identities and complete separately pinned images. Every public
call carries its pinned broker epoch, and the broker pins the worker process,
service invocation, executable, root and fixed command line. An uncertain call
withdraws the public endpoint and requires bounded fixed-unit termination,
reaping and an empty cgroup; failed cleanup cannot re-admit the old epoch. State
and completed adapter refusals survive legitimate sequential calls. No request
is replayed or silently handed to a replacement worker. Closed environments
precede Python startup in the worker/broker units.

The standalone offline bundle builder takes an externally pinned catalog, three
fully supplied Ubuntu/CPython 3.13 archives and reviewed unit templates. It never
downloads, resolves dependencies or executes image code. Exact inventories,
custom-adapter distribution metadata/entrypoint/fingerprint checks and identical
first-party package bytes preserve separate artifacts. The generated core uses
`core.app:build`; its secrets remain a separately provisioned prerequisite.
Pending tmpfiles source describes the worker socket parent needed before broker
startup. Actual mount/inventory views, loader/native closure, platform denial and
lifecycle behavior still require disposable-target qualification.

`8ab3fed554222b22d9df70477f5bf4d7295b087a` changes seven operator packaging,
manifest-parser, test and operating-documentation paths. The release builder
requires supplied native/signer/judge/baseline artifacts and exact external
catalog/release pins. It checks complete inventories, declared ELF64 dependency
edges, fixed executable modes/import slots/entry modules, explicit principals,
first-party equality and generated admission schemas. It records exact host
prerequisites without replacing host `/usr`. Native compiled principal/Python
identity and dynamic/native/SDK closure remain qualification obligations.

Both future installers write fresh immutable artifacts and `.pending`
configuration/units only. They refuse existing active/pending destinations and
use no-follow descriptor writes with exact new-directory modes independent of
umask. Existing protected directory modes remain unchanged. Supplied numeric
principals must already exist. No account provisioning, package/image execution,
service activation or provider credential enrollment occurs. Current tests use
fixture staging only; actual root installation was not executed.

The single bounded operator packaging review identified directory modes affected
by umask, incomplete fixed startup inventories and permissive unit validation.
Repairs add exact new-directory modes, fixed role/import/executable requirements
and exact reviewed unit-template binding, including rebound standalone-verifier
negative controls. The bounded gateway review identified the wrong zero-argument
factory; its repair includes a control resolving the emitted target against the
actual function signature. The later gateway umask/fsync repair was owner/root
verified and lies outside that independent review's exact byte binding. Neither
review is blanket approval of subsequent changes or installed qualification.

Final measured checks, all native exit 0:

- Gateway's own configuration: **866 passed**, zero skipped/errors/failures;
  all 84 package files unchanged during the run. Evidence: `gateway-final2.log`,
  `.xml`, `.exit` and before/after JSON under the lane. Ruff passed 52 files;
  mypy passed all seven affected production modules.
- Operator packaging/startup plus whole-tree masquerade/file-walker gates:
  **344 passed**, zero skipped/errors/failures; 533 measured package/test/gateway
  files unchanged. Evidence: `offline-final.log`, `.xml`, `.exit` and before/after
  JSON. The sole warning is the known disabled-dotenv `env_files` option.
- One tuple-formatting correction followed that frozen run. Its AST is identical;
  the final affected packaging/startup rerun passed **79 tests**, zero skipped/
  errors/failures (`offline-postformat.*`). These selections overlap.
- Owning-package/script mypy passed **147 source files**. Ruff, diff checks,
  branch safety and applicable normal commit hooks passed. All 533 final tested
  file hashes were unchanged after both source commits and hooks. The seven
  dependency files still match `0b8cfa9e` exactly.

No full engine/PostgreSQL suite, CI result, integration, installed enforcement,
provider acceptance or HIGH closure is claimed. The integration owner retains
combined qualification and merge custody; carry these commits as hunks where
its staged CLI/parser/signing repairs overlap, rather than replacing whole files.
The isolated worktree is retained. All four HIGH findings remain OPEN.

## Empty-directory custody correction for TC

TC's cumulative source review correctly retained a P1 in the baseline restoration:
`4f7ed7ac6` checked custody of file paths and their ancestors, but its census
discarded empty source directories. The subsequent gateway/packaging commits
left that code unchanged. A candidate-owned or writable empty directory could
therefore pass both custody calls and permit a new source file after the last
snapshot check, before baseline replacement.

`cca549f41283c02a8df245e2ff54c9648b0a73e3` changes only
`core/baseline_generation.py` and `test_baseline_generation.py`. Custody now asks
the existing bounded, no-follow census to include every traversed nonexcluded
directory, including empty descendants. Each receives the same owner/mode and
ancestry validation as other inputs. Ordinary snapshots remain file-only, and
the file count is bounded independently of directory entries. Existing cache
exclusions, symlink handling, byte limits and Git metadata admission remain.
The command still refuses unsafe custody; it never repairs checkout permissions.

The pre-fix run produced seven intended failures and four passing controls.
All four workflow counterexamples physically inserted a Python file after the
real final snapshot check and nevertheless published the synthetic baseline.
The fixed tests require refusal at entry or the second custody check before
that boundary, preserve original baseline bytes/mode, and leave no temporary
file. Paired controls cover protected empty directories, candidate-owned and
group/other-writable empty versus nonempty directories, unchanged file snapshots,
successful protected publication and the existing file limit. Ownership is
synthetically modelled per unsafe entry; this is not actual DAC qualification.

Final frozen validation exited 0: **118 passed**, zero errors/failures/skips,
covering baseline generation/RPC/worker/script tests, the whole-tree masquerade
gate with its positive/negative controls, and file-walker gates. Evidence is
`custody-final.log`, `.xml`, `.exit` and before/after JSON under the existing lane;
the known disabled-dotenv warning remains. All 449 measured package/test files
and HEAD were unchanged during the run. Owning-package mypy passed 145 files;
Ruff check/format, branch safety and applicable normal commit hooks passed.
The same 449 file hashes remained unchanged after the source commit and hooks.

The existing peer reviewer accepted this narrow repair against the final source
and test hashes (`custody-empty-peer-recheck.md` under the lane). It independently
inspected the defect, production callers and regression structure; it ran no
tests and granted no cumulative or runtime qualification. For TC's existing
cumulative candidate, apply only this correction after the previously selected
security inputs, preserving its unrelated staged repairs. This handoff can be
carried separately. TC's integration checkout/index were not modified here;
fresh cumulative source acceptance and merge custody remain with TC.

No installation, host/access change, provider call, real key or deployment was
used. No full engine/PostgreSQL or installed enforcement result is claimed.
All four original HIGH findings remain OPEN pending their actual qualification.
