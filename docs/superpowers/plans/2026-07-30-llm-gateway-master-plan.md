# LLM Compatibility Gateway — Master Delivery Plan

> **For agentic workers:** This is the phase-sequencing document. Each phase has (or will have) its own detailed implementation plan executed via superpowers:subagent-driven-development. Do not implement from this document alone.

**Goal:** Implement the two approved 2026-07-30 designs — the `elspeth-llm-gateway` runtime product and its ELSPETH/AWS integration — per Filigree issue `elspeth-d4b9bb20c5`.

**Specs (authoritative):**
- `docs/superpowers/specs/2026-07-30-llm-compatibility-gateway-runtime-design.md`
- `docs/superpowers/specs/2026-07-30-elspeth-llm-gateway-integration-design.md`

**Branch:** `feature/llm-gateway` (worktree `.claude/worktrees/llm-gateway`). Merge target: `release/0.7.2` with `--no-ff`.

**Branch base — corrected 2026-07-31.** This document previously claimed the branch was based on `eb7c17851` "= release/0.7.2 HEAD". That was wrong: `eb7c17851` is the design commit, and `release/0.7.2` was 24 commits ahead of it. `release/0.7.2` has since been merged INTO this branch (`c8f9947f4`) as pre-merge reconciliation, and the full CI-equivalent suite was re-run on the reconciled tree (33,662 passed) — testing either side alone would not have exercised the interaction.

## Custody amendments (binding, from sign-off)

1. **No compat shims:** profile models move OUT of `src/elspeth/web/plugin_policy/profiles.py` into the LLM domain as a flag-day move. No temporary compatibility imports.
2. **`web/_aws_ecs_acceptance/bedrock.py`** `_litellm_acompletion` use must be explicitly dispositioned (migrate or documented exemption) under integration acceptance criterion 7.
3. **Seed lowering rule:** profile lowering adds `seed` to `required_capabilities` whenever the transform config sets a seed, so mismatch fails at preflight, not per-row.
4. **Repo placement (DECIDED 2026-07-30, operator):** gateway lives in a **subdirectory of this repo** (`gateway/`) with its own `pyproject.toml`, version, and Dockerfile.

## Phase sequence (design delivery order preserved)

| Phase | Deliverable | Plan doc | Gate to next phase |
|---|---|---|---|
| 1 | `gateway/` runtime: contract major 1, adapter SDK, reference adapter, mock stack, conformance kit, Dockerfile | `2026-07-30-llm-gateway-phase1-runtime.md` (detailed, ready) | Conformance suite green in-process AND against the built container; root `pytest tests/` green |
| 2 | Shared LLM profile contract (flag-day move) + `GatewayConfig`/`GatewayLLMProvider` in the pipeline registry | `2026-07-30-llm-gateway-phase2-elspeth-provider.md` (delivered, Tasks 1-7 closed) | DELIVERED: `GatewayLLMProvider` proven end-to-end (real `uvicorn` gateway, not mocked HTTP) across all shape/usage/retry/error criteria; PH3 hash refreshed; `elspeth-lints check` exit 0, R5 ceiling untouched (61); the phase-close full-suite `pytest tests/` run surfaced two cross-cutting failures introduced by Phase 2 (`_KNOWN_AUDITED_CLIENT_USERS` missing the new gateway provider file; an unspecced `Mock()` in `test_gateway_config.py`) — both fixed and scoped re-verification (`tests/unit/telemetry/test_plugin_wiring.py`, `tests/unit/test_mock_discipline_baseline.py`, `tests/unit/plugins/llm/`) is green; full-suite `pytest tests/` re-run pending operator confirmation before merge (see task-7-report.md) |
| 3 | `ComposerCompletionClient` boundary + migration of every `_litellm_acompletion` caller (primary, advisor, guided chat, planners, boot probe, auto-title, tool dispatch; amendment 2 disposition) | `2026-07-30-llm-gateway-phase3-composer.md` (written at Phase 2 close, ready) | Composer tool round-trip against reference gateway; no un-migrated caller (`grep _litellm_acompletion` clean outside the boundary + documented exemptions) |
| 4 | Terraform shared-module generalization (`deployment lifecycle` × `llm backend` axes) + Scenario C | written at Phase 3 close | Scenario A/B contract tests unchanged-green; Scenario C plans as `first + custom_gateway` |
| 5 | Verification closeout: full suite, `elspeth-lints check`, local reference acceptance, evidence | checklist in this doc | All green → merge `--no-ff` to release/0.7.2 |

**Operator-gated (NOT in agent scope):** live AWS disposable acceptance for Scenario C (account is greenfield post-GLM-5 teardown); trust-tier judge/sign sweep (release boundary, per standing gate-debt posture); secret creation/rotation.

## Cross-phase constraints

- TDD throughout; the plain `pytest tests/` selection is the CI-equivalent gate and must stay green at every phase boundary (scoped runs miss cross-cutting gates).
- No venv mutation from the worktree (`.venv` is a symlink to the main checkout). Phase 1 wires gateway code into root tests via `sys.path` in a conftest, NOT via editable install; converting `gateway/` to a uv workspace member is a merge-boundary decision for the operator.
- Gateway logs are metadata-only: no prompt, response, schema, tool argument, credential, token, or raw upstream error may appear in any log or error string. Every phase adds non-leak tests for the surfaces it touches.
- Direct Azure/OpenRouter/Bedrock providers keep current behavior; every phase re-runs their tests.
- Plugin edits under `src/elspeth/plugins/` require a PH3 `source_file_hash` refresh (CI-only gate) — Phase 2 includes it.
- New imports rotate tier-model fingerprints; expect signature CI churn mid-release and leave it to the release-boundary sign sweep (do NOT attempt judge/signing; [O1] custody rule).
