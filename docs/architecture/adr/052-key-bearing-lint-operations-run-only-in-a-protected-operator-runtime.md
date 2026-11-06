# ADR-052: Key-Bearing Lint Operations Run Only in a Protected Operator Runtime

**Date:** 2026-10-24
**Status:** Proposed
**Deciders:** ELSPETH maintainer
**Review evidence:** Codex security lane, `docs/security/dot10-security-handoff.md` and `docs/security/dot10-runtime-artifact-review.md` (advisory; their claims of operator approval are uncorroborated); 0.8.10 unit U-10 measurements
**Tags:** elspeth-lints, trust-tier, judge-signature, key-custody, operator-runtime, related-adr-046

## Context

The `trust_tier.tier_model` allowlist seals each judge-gated suppression with an
HMAC signature made with the operator-held `ELSPETH_JUDGE_METADATA_HMAC_KEY`.
Until 0.8.10 the operator signed by running the editable `elspeth-lints` CLI
from a checkout, in a shell that held the key. That process could be steered by
the very thing it was signing:

- **Checkout-controlled startup** (`csf_652f5deeb8d970ec214048eb`). The
  interpreter, `PYTHONPATH`, user-site, `.pth` files and imported modules all
  came from the checkout under review.
- **Keyed repository maintenance** (`csf_49e6c1d4c587149b07c83516`). Git
  subprocesses ran in the keyed process with the inherited environment, so
  repository filters, fsmonitor and include callbacks could execute candidate
  code. The signer also ran automatic maintenance after signing.
- **Provider SDK in the keyed parent** (`csf_e22b5e22120644ec52c10bfd`). The
  Claude SDK and other judge transports were imported into the process that
  held the key.

ADR-046 keeps signed allowlists, judge signatures and the [O1] key-custody rule
at audit grade. These three findings are product-integrity defects, not
tooling hygiene.

The maintainer has also suspended signing pending the on-prem migration
(`docs/release/0.8.10-custody-handover-prompt.md` § 2). No sign-bundle, rekey
or baseline regeneration runs in 0.8.10. Findings are tracked by the keyless
trust-tier set-diff.

## Decision

**The trust boundary for every key-bearing `elspeth-lints` operation is a
separately installed, protected operator runtime entered only through the fixed
native entrypoint `/usr/local/libexec/elspeth-operator`, with artifacts under
`/opt/elspeth/operator`.** The editable checkout is never trusted with the key.

Concretely, as shipped in 0.8.10 source (commits `ec6fe2c81 eed99de69
108ad8b01 f16cc411b adb27d441 4f7ed7ac6 8ab3fed55 cca549f41`, cherry-picked):

1. **The editable CLI refuses.** `sign-bundle`, `rekey`,
   `sign-judge-signatures`, `migrate-judge-scope`,
   `regenerate-fingerprint-baseline`, non-dry-run `justify` and
   `audit-verdict`, `diagnose-judge-signatures --env-file`, and any command run
   with the key variable present call `operator_runtime.require_operator_runtime()`
   immediately after argument parsing (`main()`). With no protected runtime established, each exits 2
   with `Operator runtime unavailable: key-bearing operation requires the
   protected operator runtime: /usr/local/libexec/elspeth-operator`
   (`elspeth-lints/src/elspeth_lints/core/cli.py`).
2. **Native admission precedes Python.** The static launcher
   (`elspeth-lints/operator-runtime/launcher.c`) checks ownership, modes and
   resolved paths. It then executes a fixed interpreter with an isolated import
   path, no site processing and a closed environment, passing only the selected
   signer values in a sealed descriptor. A forged or unqualified admission
   exits 78. The default build admits no identities and refuses.
3. **Candidate bytes are data.** The signer parses candidate source, bundles
   and allowlists. It never imports them. Git is observed through a
   callback-free, config-free metadata view (`core/trusted_git.py`) rather than
   bare `git` with the inherited environment. Automatic post-signing repository
   maintenance is removed.
4. **Provider code runs outside the keyed process.** Judge calls go through
   bounded, versioned JSON RPC to separately supervised workers
   (`core/judge_rpc.py`, `judge_supervisor.py`, `judge_worker.py`). The keyed
   process refuses to import the Claude SDK. Baseline capture runs in a
   separate key-free worker (`core/baseline_*.py`).
5. **Key-free paths are unchanged in kind.** The keyless
   `elspeth-lints check` (shape-only verify mode) and the `elspeth-judge` MCP
   staging tools stay usable by agents and CI.

This runtime is operator-host tooling. `elspeth-lints` is a dev/test-group
dependency that `src/elspeth` never imports, and nothing here is deployed to AWS
ECS or Azure Container Apps.

## Consequences

### Positive Consequences

- A checkout under review can no longer choose the code that holds the signing
  key, run callbacks in that process, or import a provider SDK beside it.
- The refusal is unconditional and comes first: an agent or CI shell that runs a
  key-bearing verb from the checkout fails before any key, bundle or file is
  read.
- The three signer findings now have a concrete closure path: install and
  qualify the runtime.

### Negative Consequences

- **No supported way to sign exists today.** The protected runtime is source
  only. It is not built, installed or qualified on any host. Together with the
  suspension, signed allowlist drift accumulates until the post-release operator
  work is done.
- The source profile is Ubuntu x86-64 with systemd-supervised workers and fixed
  `/opt`, `/etc`, `/run` and `/usr/local/libexec` paths. It binds the signing
  host to that profile.
- `8ab3fed55` also packages worker roles (for example OpenRouter) and systemd
  units whose gateway consumer (`8f5619255`, `d993e6d26`) is dropped from
  0.8.10. That part of the packaging has no consumer.
- The `csf_49e6…`, `csf_652f…` and `csf_e22b…` findings stay **OPEN** until the
  runtime is installed and qualified. Source and tests with synthetic
  admission do not prove installed startup, native closure or OS enforcement.

### Neutral Consequences

- CI already never signs (`.github/workflows/enforce-allowlist-judge-gates.yaml`,
  guarded by `tests/unit/elspeth_lints/test_meta_ci_never_signs.py`). This ADR
  does not change CI.
- Agents still never hold the key ([O1]). What changes is that the operator no
  longer signs from an editable checkout either.

## Alternatives Considered

### Alternative 1: Keep signing from the editable CLI, with hardening flags

**Description:** Run the existing CLI with `python -I`, a stripped environment,
`--no-ext-diff` and a pinned SDK.

**Rejected because:** The checkout still supplies the interpreter's imports and
the Git configuration. Clean/process filters and fsmonitor run regardless of
`--no-ext-diff`. Stripping known variable names cannot cover arbitrary rekey
names. None of this separates provider code from the key.

### Alternative 2: Sign in CI with a protected secret

**Description:** Hold the key as a CI secret and sign on a protected branch.

**Rejected because:** It contradicts [O1] and the standing CI-never-signs guard.
Workflow-controlled code would be able to reach the key.

### Alternative 3: Suspend signing permanently and rely on the keyless set-diff

**Description:** Drop signed judge metadata entirely.

**Rejected because:** This is not decided. The maintainer suspended signing
pending a migration, not permanently. The keyless set-diff detects new findings,
but it cannot detect forged or tampered judge metadata (shape-only mode warns
about exactly this).

## Related Decisions

- ADR-043: shared development tooling.
- ADR-046: audit grade is a product characteristic. Signed allowlists and [O1]
  remain product surfaces.

## References

- `elspeth-lints/operator-runtime/README.md`
- `elspeth-lints/judge-runtime/ubuntu/README.md`
- `elspeth-lints/baseline-runtime/ubuntu/README.md`
- `elspeth-lints/src/elspeth_lints/core/operator_runtime.py` (`OPERATOR_ENTRYPOINT`, `require_operator_runtime`)
- `docs/judge-signature-handoff.md`
- `docs/security/dot10-security-handoff.md` (annotated for 0.8.10)
- `AGENTS.md` § Operator signature verification

## Notes

Status is **Proposed**. The refusal behaviour ships in 0.8.10 source. The
installed-runtime design it points to has not been qualified, and its earlier
"operator approved" records are uncorroborated. The maintainer should accept,
amend or supersede this ADR when signing is re-enabled after the on-prem
migration. Building, installing and qualifying the runtime and judge
supervisor, and re-enabling signing, are post-release operator work.
