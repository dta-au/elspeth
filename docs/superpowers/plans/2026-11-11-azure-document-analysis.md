# Plan: Azure document analysis (Document Intelligence fixes + Content Understanding)

Spec: [2026-11-11-azure-document-analysis-design.md](../specs/2026-11-11-azure-document-analysis-design.md)

## Steps

1. **Shared core.** Extract `analyze_lro.py` (config base, transform base,
   submit/poll/retry, audit projection, probe) and `analyze_result.py` (shared
   Tier-3 parsers) out of `document_intelligence.py`. Existing DI tests stay
   green; retarget the `time` monkeypatch to `analyze_lro`.
2. **DI fixes.** One test per audit finding (1–10), then a mutation check that
   reverts each fix and confirms a test goes red.
3. **CU transform.** `content_understanding.py` + `content_understanding_result.py`
   on the shared core; unit tests mirroring the DI suite; ADR-009 contract test;
   protected live PB-09 test (skips when unconfigured) + env names in
   `tests/integration/plugins/_azure_live.py`.
4. **Registration and whole-tree pins** (CONTRIBUTING § new plugin checklist):
   `run_mode_capabilities`, discovery and catalog counts, external catalogue
   metadata, validation-path agreement, invariants tables, knob-schema golden,
   contracts whitelist, audit-readiness determinism + explain text, frontend
   display name and binding label, state-engine matrix + proof catalogs,
   source hashes last.
5. **Docs.** Configuration reference, environment variables, user manual,
   YAML authoring guide, CHANGELOG.
6. **Gates.** ruff, mypy, contracts, trust-tier corpus diff (before/after on
   exported trees), `trust_boundary.*` CI rules, affected tests, then
   `scripts/full-suite-gate.sh --execute --detach`.
7. **Ship.** `scripts/branch-safety-check.sh`, commit, push to
   `claude/vibrant-bardeen-ylh2hw`.
