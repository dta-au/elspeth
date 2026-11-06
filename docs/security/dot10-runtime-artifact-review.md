# .10 supervised runtime and offline artifact source review

> **0.8.10 assembly annotation (2026-10-24, unit U-10).** This is a Codex
> security-lane review, carried verbatim from WIP preserve commit `255f1cff3`;
> this note is the only change. The gateway increment `d993e6d26` reviewed
> below (and its predecessor `8f5619255`) is **dropped** from 0.8.10 by
> maintainer ruling. The gateway adapter finding `csf_cfe54ceaf27dac2419e05855`
> is closed by design in `c6a9fea80` (landed as `b04ba8532`), so the "Gateway
> controls established in source" section and the gateway test limits describe
> code 0.8.10 does not ship. The operator-packaging and baseline-custody
> sections apply to the carried commits `8ab3fed55` and `cca549f41`. Signing is
> suspended, so no part of this review authorizes a sign, rekey or baseline
> regeneration.

**Cumulative source GO through `f3e6862ed1906cc2c272c0326b062b0fd5aefe75`, with production correction `cca549f41283c02a8df245e2ff54c9648b0a73e3`.** The sole previously reported P1 empty-source-directory omission is resolved in committed source. No surviving integration-blocking finding was established in this bounded cumulative review. This permits the integration owner to reconcile the reviewed source; it does not establish that integration has occurred. All four original HIGH findings remain OPEN pending actual target/runtime qualification. No installation, signing, provider use, security activation or deployment is authorized by this verdict.

## Superseding custody recheck

The earlier NO-GO below remains the historical verdict for `8ab3fed554222b22d9df70477f5bf4d7295b087a`. Immutable Git history shows the subsequent chain:

```text
8ab3fed554222b22d9df70477f5bf4d7295b087a
d983401b2704783ea048aaf796fccbbb0ec69e47  documentation only
cca549f41283c02a8df245e2ff54c9648b0a73e3  custody source and regression tests
f3e6862ed1906cc2c272c0326b062b0fd5aefe75  documentation only
```

The cumulative changed-path inventory after `8ab3fed5` contains only `docs/security/dot10-security-handoff.md`, `elspeth-lints/src/elspeth_lints/core/baseline_generation.py`, and `tests/unit/elspeth_lints/test_baseline_generation.py`. Thus the previously reviewed Gateway and runtime-packaging code remains unchanged. The handoff is context, not proof of its claims.

At `cca549f41283c02a8df245e2ff54c9648b0a73e3`, `baseline_generation.py:100–102` requests `_census(..., include_directories=True)`. Lines 167–170 add every traversed nonexcluded directory itself, including empty descendants, to the returned custody paths. The existing owner/mode/ancestry checks at lines 90–98 now reject the previously omitted candidate-owned `src/elspeth/new_area` independently of whether it contains a file. Admission still runs at entry (line 438) and after worker/reverification work (line 461), before final observation/publication. This removes the concrete writer precondition of the reported insertion race. It remains an externally protected-custody design; it does not introduce an atomic source snapshot or establish real OS permission enforcement.

`include_directories` defaults to false (`:146`), and `freeze_inputs()` retains its file-only call (`:190`). The traversal bound remains at lines 158–160; a separate file counter at lines 171–175 preserves the prior file limit when directories are included. Existing exclusions and no-follow handling remain unchanged. The correction does not execute candidate code, alter persisted schemas, relax signing checks, or repair filesystem permissions.

The new tests at `test_baseline_generation.py:294–411` retain an individually unsafe entry after normalizing surrounding metadata. They cover candidate ownership and group/other writability for empty/nonempty directories, protected-empty acceptance and unchanged snapshots, and the existing file limit. The workflow regression restores the captured production custody function (`:387`), then places an insertion hook after the actual final `_unchanged()` and before replacement (`:394–400`; production `:413–421`). Fixed code must refuse before that hook, at entry or after the worker, preserving baseline bytes/mode and leaving no temporary file (`:404–411`). This is a meaningful source regression against the reported mechanism. Scans, runtime admission and worker behavior remain partly stubbed; it is not native DAC or signer qualification.

I read the complete owner-lane `custody-final.log` and its explicit `.exit`, rather than relying on the handoff's result claims. Raw terminal evidence:

```text
custody-final.exit: 0
118 passed, 1 warning in 81.20s (0:01:21)
cmp custody-final-before.json custody-final-after.json: exit 0, no output
```

The warning is the existing unknown `env_files` configuration option. The recorded source/test SHA-256 values equal hashes independently computed from the committed Git blobs:

```text
6a8d891df462568e986c988c44c0d1be28951722b9536222eeea5d61a83a7258  baseline_generation.py
0e504fb2c5ec8cd4a7a8798d1ad7959e2a012fb3ec064f13566c2a5981449e78  test_baseline_generation.py
```

Both files have identical Git blob identities in `cca549f4` and `f3e6862`: `a846ecb7dfbc96dd96cac175938d14fd151cbc28` and `1a25e4a46f1ea5d34abe6f5094083d8ea6978e69`, respectively. This is inspected existing focused-test evidence, not a reviewer-executed run or combined integration gate. The existing peer recheck was read for counterarguments; the verdict above follows the committed source and test mechanism directly. Integration HEAD remained `51bb8131723baa822c1ce65ce1a981722d6c90f6` during this recheck. Only this report was updated; no source/test/index/ref/object mutations or runtime operations occurred.

## Historical review through 8ab3fed5 — disposition superseded above

The historical verdict was NO-GO because the empty-directory defect remained present at that tip. No additional blocker was established in the Gateway or packaging deltas. Their source controls and runtime limitations below remain relevant; statements that the P1 remains or must be repaired apply only to that earlier tip.

Reviewed immutable objects:

- Gateway increment: `d993e6d26df95c23946da8567fbe9f2296b90a91`, parent `4f7ed7ac6563629fbf1db67a4396a88c3c29a8d5`.
- Operator/worker packaging increment: `8ab3fed554222b22d9df70477f5bf4d7295b087a`, parent `d993e6d26df95c23946da8567fbe9f2296b90a91`.

Integration HEAD was read as `51bb8131723baa822c1ce65ce1a981722d6c90f6`. Review used immutable Git objects and the earlier coordination review. No candidate working files supplied source evidence. No Git index/ref/object mutation, candidate import, test, native execution, network/provider call, installation, service action or key handling occurred. `git check-ignore -v docs/security/dot10-runtime-artifact-review.md` returned no output, exit 1, before writing this sole permitted deliverable.

## Historical P1: empty source directories escaped custody admission

References in this section bind to `8ab3fed554222b22d9df70477f5bf4d7295b087a`, file `elspeth-lints/src/elspeth_lints/core/baseline_generation.py`.

`require_publication_custody()` validates the checkout root and `_census()` results at lines 100–102. `_census()` visits directories but returns only nondirectory entries at lines 146–174, specifically 166–169. A candidate-owned empty source directory has no returned leaf whose ancestry would trigger its ownership check. Git metadata is separately walked at lines 110–121; source directories are not.

Concrete source-level counterexample: all checkout ancestors, tracked files and Git metadata are root/operator-owned and nonwritable by other principals; an empty `src/elspeth/new_area` alone belongs to the candidate UID, mode 0755. It escapes both custody calls (lines 434 and 457), and has no tracked or census file entry. After `_publish()` performs its last `_unchanged()` at line 409, the candidate creates a Python file there before unconditional `os.replace()` at line 417. The intervening parent identity check concerns the baseline destination, not the source directory. The private generator lock cannot exclude an ordinary filesystem writer.

This is a source-derived feasible interleaving, **not an executed race reproduction**. Changes visible before the final snapshot check still refuse; nonempty candidate-owned directories still fail ancestor checks. Neither countercontrol covers the empty-directory window.

Raw `git ls-tree` evidence at the previous reviewed commit and new tip:

```text
4f7ed7ac6563629fbf1db67a4396a88c3c29a8d5
100644 blob 5cbdb194426cc9c24437f1c03eb20ba0c1efe81d elspeth-lints/src/elspeth_lints/core/baseline_generation.py
8ab3fed554222b22d9df70477f5bf4d7295b087a
100644 blob 5cbdb194426cc9c24437f1c03eb20ba0c1efe81d elspeth-lints/src/elspeth_lints/core/baseline_generation.py
```

The existing implementation owner must validate every traversed source directory, including empty descendants, retaining refusal rather than changing checkout ownership. Require paired protected-empty-directory acceptance and candidate-owned/group-writable empty-directory refusal controls, followed by attempted insertion at the final check/publication boundary with baseline-preservation assertions. The earlier coordination report contains the detailed recipe; this increment does not repair it.

## Gateway controls established in source

References below bind to `d993e6d26df95c23946da8567fbe9f2296b90a91`; paths start at `gateway/src/elspeth_llm_gateway/` unless otherwise stated.

- `core/runtime_artifact.py:199–237,259–303` authenticates root-owned ancestry, refuses symlinks and multiply linked files, compares complete three-role inventories and exact unit digests, and validates every enumerated artifact directory, including empty directories. This separate implementation does not fix baseline custody.
- `adapter_supervisor.py:229–273,319–358,440–464` binds MainPID and InvocationID, obtains a pidfd, checks executable/root/cgroup/arguments and private socket peer, and rejects stale/unpinned later epochs before forwarding. Calls are serialized; only initial descriptor/capabilities requests may omit the epoch. Uncertainty poisons the epoch; fencing precedes cleanup at lines 360–368 and 483–521. Stop completion requires inactive/failed service, zero MainPID, no job and empty cgroup at lines 208–227 and 275–289. These paths do not automatically replay or replace an uncertain call.
- `core/adapter_client.py:183–223,230–262,380–399` binds the root socket peer and response epoch and invalidates failed calls. Process checks deliberately accept `PermissionError`; exact broker process identity is therefore not proved in every host configuration. That branch relies on the protected endpoint, root kernel peer, externally pinned artifacts and trusted root service custody.
- `runtime_bundle.py:117–179,250–320,365–391,455–498,553–609` treats archives as data, checks external catalog/archive pins, reconciles trusted inventories and adapter-distribution bytes, and writes pending admission/unit files. Root installation checks fixed destinations and accounts; descriptor-relative output refuses writable or symlinked parents. Supplied modules are not imported. ELF checks are header checks, not executable/native-closure qualification. External catalog provenance and installer trust remain prerequisites.

The worker requests nonroot execution, private networking, no capabilities and cgroup killing (`gateway/deploy/ubuntu/elspeth-gateway-adapter-v2.service:10–57`); the broker has separate root/manager authority (`gateway/deploy/ubuntu/elspeth-gateway-supervisor.service:10–47`). These are requested settings, not measured isolation. The v1 admission break is explicit in `gateway/README.md:55–58`.

## Operator packaging controls established in source

References below bind to `8ab3fed554222b22d9df70477f5bf4d7295b087a`; `core/` means `elspeth-lints/src/elspeth_lints/core/`.

`core/runtime_artifacts.py:152–193,206–315,353–404,609–681` bounds ordinary reads, refuses link/traversal/startup-hook forms, checks external digests, and parses ELF declarations without loading supplied code. Declared dependencies and shebang references are checked; dynamic loading and actual executability remain outside that proof.

`core/runtime_artifacts.py:413–446,464–584,684–760` checks principal separation, fixed entry/import paths, signer/baseline-worker first-party byte equality, exact unit-template hashes, complete inventories and regenerated configuration equality. Standalone verification repeats these semantic checks. Root installation at lines 825–948 requires existing principals and protected matching host prerequisites, uses exclusive descriptor-relative writes, publishes configs/units as `.pending`, and attempts inode-bound rollback on failure. It does not download, start services or load installed code.

The delta does not add mutable candidate execution to the key-bearing path. The extracted manifest parser remains a data parser (`core/operator_runtime.py:155–227`); native admission still precedes CLI dispatch in `elspeth-lints/operator-runtime/bootstrap.py:17–33`. A supplied static ELF is not thereby proven to be the reviewed native launcher; protected storage alone does not prove source equivalence. Independently authenticated build inputs/installer, binary-to-source binding and host startup qualification remain necessary. Native launcher files are installed at their executable destination: `.pending` describes service/config admission, not a universal execution interlock.

## Test-source limits and remaining counterexamples

No tests ran here. Actual added test source supports narrower claims than native qualification:

- Gateway lifecycle tests use same-account children and a replacement lifecycle (`gateway/tests/test_adapter_supervisor.py:91–159`); systemd/PID/cgroup cases use fake status/filesystem fixtures at lines 409–563. They exercise uncertainty and epoch logic, not real namespace enforcement, credential exclusion, service completion or descendant reaping. Qualify exact unit mount views, private-directory lifecycle, peer/proc observations and cgroup teardown on the target host.
- Gateway artifact tests rewrite `fstat` ownership and directory write bits (`gateway/tests/test_runtime_artifact.py:33–43`). Add an individually unsafe empty directory beneath otherwise protected ancestry without globally masking its metadata.
- Gateway bundle tests use synthetic ELF headers and deliberately nonexecutable Python bodies (`gateway/tests/test_runtime_bundle.py:30–53`). Operator packaging uses synthetic ELF and placeholder packages (`tests/unit/elspeth_lints/test_runtime_release.py:46–98`; `tests/unit/elspeth_lints/test_runtime_artifacts.py:39–83`). Those are useful parser/no-execution controls, not interpreter, dependency, SDK or launcher qualification.
- Root installation remains unexercised by these added tests. Test preservation/recovery for failures during write, chmod/chown/fsync and midway through publication. Gateway installation has no rollback after partial inactive writes (`runtime_bundle.py:578–582`); operator installation has explicit rollback whose identity/failure branches need testing. Pending configuration does not remove partial-install recovery obligations.

Historical disposition: return the P1 to the existing security implementation owner without duplicating the repair lane. That owner has now repaired it in `cca549f41283c02a8df245e2ff54c9648b0a73e3`, as independently rechecked above. All four original HIGH findings remain open; source acceptance is distinct from installation, provider calls, operator-key use, activation or deployment.
