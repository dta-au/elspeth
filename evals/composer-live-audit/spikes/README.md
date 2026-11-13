# Spike instruments (2026-11-13)

Artifacts the remediation lanes need, kept from the design spikes. The design
notes are in `docs/development/composer-desk-audit-2026-11-12/spikes/`.

| Path | Used by | What it is |
|---|---|---|
| `S1-advisor-replay/replay.py` | CDA-07 acceptance | Replays captured GLM-5.3 END-advisor requests directly against OpenRouter in arms A (unchanged), B (with capability record and rubric sentence) and C (B with a planted false claim). Run with `python replay.py run <arm> <n> [case …]`; results append to `results/<arm>.jsonl`. |
| `S1-advisor-replay/requests/` | CDA-07 acceptance | The prebuilt request variants: W1–W4 withheld-correct, K1–K2 admitted controls, C1–C5 false claims. Each embeds the full advisor system text from `1a7ff0e6`. `python replay.py build` regenerates them, but only from the original live captures, which are not in git. |
| `S1-advisor-replay/build_record.py`, `record_*.json` | CDA-07 | Builds the "current deployment capabilities" record through `advisor_abandonment` for the audited instance configuration. CDA-07's renderer must produce the same bytes. |
| `S1-advisor-replay/results/` | Reference | The spike's raw per-call verdicts (baseline for comparison). |
| `S2-planner-overlay/analyse.py` | CDA-12 acceptance | The controlled strict palette-confusion counter (`strict_hits`), calibrated to 32/110 on the audit corpus, and the per-arm turn summary. |
| `S2-planner-overlay/prototype.patch` | CDA-12 starting point | The planner-overlay prototype, against `61382f68`-era `src/`. |
| `S3-multifile-custody/prototype.patch` | CDA-13 starting point | Named-source `blob_id` binding through `_resolve_source_blob`, the `proposal_blob_refs` fix, and tests. |
| `S3-multifile-custody/s10_seq12_pipeline.json` | CDA-13 | The planner's own s10 attempt, used as a fixture. |

Re-measuring a red-on-base test from a worktree: pytest's configured
`pythonpath` (`pyproject.toml`) silently overrides an exported `PYTHONPATH`.
Pass `-o pythonpath=<root>/src:<root>/elspeth-lints/src`, and check
`elspeth.__file__` and `elspeth_lints.__file__`.
