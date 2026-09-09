"""Build the bucket manifest and per-lane briefs from ``blanket_census.py`` output.

Run from the repository root after the census::

    python docs/plans/2026-08-30-per-file-blanket-migration-tools/bucket.py census.json

Writes ``docs/plans/2026-08-30-per-file-blanket-migration.buckets.json`` and
one brief per bucket under ``docs/plans/2026-08-30-per-file-blanket-migration-briefs/``.
Bucket membership is the table in the plan document (BUCKETS below); this
script only attaches the live finding list to each file and refuses to write
anything if the census and the table disagree.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

PLAN = "docs/plans/2026-08-30-per-file-blanket-migration.md"
OUT_JSON = Path("docs/plans/2026-08-30-per-file-blanket-migration.buckets.json")
BRIEF_DIR = Path("docs/plans/2026-08-30-per-file-blanket-migration-briefs")

# (bucket, wave, model, files)
BUCKETS: list[tuple[str, int, str, list[str]]] = [
    ("C01", 1, "opus", ["core/templates.py"]),
    ("C02", 1, "opus", ["core/expression_parser.py", "core/canonical.py", "core/checkpoint/serialization.py", "contracts/hashing.py"]),
    ("C03", 1, "fable", ["core/config.py", "core/secrets.py", "core/security/config_secrets.py", "core/security/secret_loader.py"]),
    (
        "C04",
        1,
        "opus",
        [
            "contracts/runtime_val_manifest.py",
            "contracts/freeze.py",
            "contracts/type_normalization.py",
            "contracts/contract_records.py",
            "contracts/call_data.py",
            "contracts/events.py",
        ],
    ),
    (
        "C05",
        1,
        "opus",
        [
            "contracts/schema.py",
            "contracts/config/runtime.py",
            "contracts/plugin_context.py",
            "contracts/token_usage.py",
            "contracts/results.py",
        ],
    ),
    (
        "C06",
        1,
        "opus",
        [
            "contracts/audit.py",
            "contracts/secret_scrub.py",
            "contracts/header_modes.py",
            "contracts/diversion.py",
            "tui/screens/explain_screen.py",
            "tui/widgets/node_detail.py",
            "tui/widgets/lineage_tree.py",
            "testing/__init__.py",
        ],
    ),
    ("E01", 1, "opus", ["engine/processor.py"]),
    (
        "E02",
        1,
        "opus",
        [
            "engine/token_traversal.py",
            "engine/triggers.py",
            "engine/scheduler_drain.py",
            "engine/executors/gate.py",
            "engine/executors/transform.py",
            "engine/dag_navigator.py",
        ],
    ),
    ("E03", 1, "opus", ["engine/barrier_coordination.py", "engine/tokens.py", "engine/batch_adapter.py", "core/landscape/formatters.py"]),
    ("L01", 1, "opus", ["core/landscape/database.py", "core/landscape/journal.py", "core/operations.py"]),
    ("L02", 1, "opus", ["core/landscape/run_lifecycle_repository.py", "core/dag/graph.py"]),
    ("P01", 2, "opus", ["cli.py", "cli_helpers.py"]),
    ("P02", 2, "opus", ["mcp/server.py", "mcp/analyzers/queries.py", "mcp/analyzers/diagnostics.py", "mcp/analyzers/reports.py"]),
    (
        "P03",
        2,
        "opus",
        [
            "composer_mcp/server.py",
            "plugins/infrastructure/config_base.py",
            "plugins/infrastructure/schema_factory.py",
            "plugins/infrastructure/clients/json_utils.py",
            "plugins/infrastructure/clients/llm.py",
            "plugins/infrastructure/clients/replayer.py",
            "plugins/infrastructure/utils.py",
        ],
    ),
    (
        "P04",
        2,
        "fable",
        [
            "plugins/transforms/llm/transform.py",
            "plugins/transforms/llm/validation.py",
            "plugins/transforms/llm/providers/bedrock.py",
            "plugins/transforms/llm/multi_query.py",
            "plugins/transforms/llm/image_inputs.py",
            "plugins/transforms/llm/templates.py",
            "plugins/transforms/llm/providers/azure.py",
            "plugins/transforms/llm/provider.py",
            "plugins/transforms/llm/langfuse.py",
        ],
    ),
    (
        "P05",
        2,
        "fable",
        [
            "plugins/transforms/azure/document_intelligence.py",
            "plugins/transforms/azure/document_intelligence_result.py",
            "plugins/transforms/azure/content_safety.py",
            "plugins/transforms/azure/base.py",
        ],
    ),
    (
        "P06",
        2,
        "opus",
        [
            "telemetry/exporters/otlp.py",
            "telemetry/exporters/datadog.py",
            "telemetry/exporters/azure_monitor.py",
            "telemetry/exporters/console.py",
            "telemetry/serialization.py",
            "telemetry/factory.py",
        ],
    ),
    ("P07", 2, "fable", ["plugins/sources/aws_s3_source.py", "plugins/sources/azure_blob_source.py", "plugins/sources/csv_source.py"]),
    (
        "P08",
        2,
        "fable",
        [
            "plugins/sources/llm/source.py",
            "plugins/sources/json_source.py",
            "plugins/sources/dataverse.py",
            "plugins/sinks/dataverse.py",
            "plugins/infrastructure/clients/dataverse.py",
            "plugins/infrastructure/clients/fingerprinting.py",
        ],
    ),
    (
        "P09",
        2,
        "opus",
        [
            "plugins/sinks/json_sink.py",
            "plugins/sinks/chroma_sink.py",
            "plugins/transforms/safety_utils.py",
            "plugins/transforms/type_coerce.py",
            "plugins/transforms/keyword_filter.py",
            "plugins/transforms/batch_stats.py",
            "plugins/transforms/json_explode.py",
        ],
    ),
    ("W01", 3, "opus", ["web/composer/guided/chat_solver.py"]),
    ("W02", 3, "opus", ["web/composer/tools/_common.py", "web/composer/telemetry_phase8.py"]),
    ("W03", 3, "opus", ["web/composer/tool_batch.py", "web/composer/service.py"]),
]

BRIEF = """# Lane {bucket} — wave {wave} — model `{model}`

**CWD:** `.claude/worktrees/tier-{bucket_lc}` (create it: `git worktree add .claude/worktrees/tier-{bucket_lc} -b tier-{bucket_lc} tier/blanket-burndown`, then `ln -s "$(git -C . rev-parse --show-toplevel)/.venv" .venv` — the symlink target is the MAIN checkout's venv). Export `PYTHONPATH=$PWD/src:$PWD/elspeth-lints/src` and verify both `elspeth.__file__` and `elspeth_lints.__file__` resolve inside the worktree before trusting any result.

**Read first:** `{plan}` (§ Global Constraints, § Disposition classes, § Lane contract — binding verbatim), `docs/agents/recent-code-hints.md` (2026-08-29 `@trust_boundary` entries), ADR-032, `src/elspeth/contracts/trust_boundary.py` docstring, `CONTRIBUTING.md § Whole-tree gates`.

**Do NOT edit:** `config/cicd/enforce_tier_model/*.yaml`, `.elspeth/`, `elspeth-lints/`. Do NOT commit to the shared checkout. Never hold `ELSPETH_JUDGE_METADATA_HMAC_KEY`.

## Your findings ({total})

Every line below is a finding that surfaces the moment the hub deletes your files' blankets. Each must end as **F** (fixed — pattern gone from the raw corpus), **D** (`@trust_boundary`/`@observation_boundary`, honest Tier-3, param-rooted, with `test_ref`+`test_fingerprint` or `non_raising`), or **J** (per-site rationale in `docs/agents/sweeps/tier-burndown/{bucket}.rationales.json`, key exactly as `elspeth-lints check` prints it: `file:RULE:symbol:ast=<path>`).

{expectation}

{findings}

## Evidence you hand back (Filigree comment on your bucket issue + message to the hub with the branch name)

- raw corpus count before/after: `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules trust_tier.tier_model --root src/elspeth --allowlist-dir <copy of config/cicd/enforce_tier_model with per_file_rules: [] in every file> 2>&1 | grep -cE '^[a-z0-9_/]+\\.py:[0-9]+:'` — COUNT, never `tail`.
- if you added any decorator: `elspeth-lints check --rules trust_boundary.tests,trust_boundary.scope,trust_boundary.tier --root src/elspeth` clean (CI-only gate; a green pytest proves nothing about it).
- `pytest -n 2 tests/unit/<your area>` green.
- counts `fixed + decorated + justified == {total}`, listing each key under its class.
"""


def main(census_path: str) -> int:
    census = json.loads(Path(census_path).read_text(encoding="utf-8"))
    by_file: dict[str, dict[str, int]] = census["by_file"]
    lines_by_file: dict[str, list[str]] = defaultdict(list)
    for entries in census["by_blanket"].values():
        for line in entries:
            file_path = line.rsplit(":", 2)[0]
            lines_by_file[file_path].append(line)

    assigned: Counter[str] = Counter()
    manifest = []
    for bucket, wave, model, files in BUCKETS:
        rows = []
        for file in files:
            assigned[file] += 1
            rules = by_file.get(file)
            if rules is None:
                sys.exit(f"{bucket}: {file} has no blanket-covered findings in the census (dead table row?)")
            loc = (Path("src/elspeth") / file).read_text(encoding="utf-8").count("\n")
            rows.append({"file": file, "loc": loc, "findings": sum(rules.values()), "rules": rules})
        manifest.append(
            {
                "id": bucket,
                "wave": wave,
                "model": model,
                "loc": sum(r["loc"] for r in rows),
                "findings": sum(r["findings"] for r in rows),
                "files": rows,
            }
        )

    unassigned = sorted(set(by_file) - set(assigned))
    doubled = sorted(f for f, n in assigned.items() if n > 1)
    total = sum(b["findings"] for b in manifest)
    if unassigned or doubled or total != census["standing_on_blankets"]:
        sys.exit(f"RECONCILE FAIL unassigned={unassigned} doubled={doubled} sum={total} standing={census['standing_on_blankets']}")

    OUT_JSON.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    BRIEF_DIR.mkdir(exist_ok=True)
    for b in manifest:
        blocks = []
        for row in b["files"]:
            lines = sorted(lines_by_file[row["file"]], key=lambda s: int(s.rsplit(":", 2)[1]))
            blocks.append(
                f"### `{row['file']}` ({row['loc']} LOC; {row['findings']} — {row['rules']})\n\n```\n" + "\n".join(lines) + "\n```"
            )
        (BRIEF_DIR / f"{b['id']}.md").write_text(
            BRIEF.format(
                bucket=b["id"],
                bucket_lc=b["id"].lower(),
                wave=b["wave"],
                model=b["model"],
                plan=PLAN,
                total=b["findings"],
                expectation=f"Expected disposition for this bucket: see the wave-{b['wave']} table row for {b['id']} in the plan.",
                findings="\n\n".join(blocks),
            ),
            encoding="utf-8",
        )
    for wave in (1, 2, 3):
        bs = [b for b in manifest if b["wave"] == wave]
        sys.stderr.write(f"wave {wave}: {len(bs)} lanes, {sum(b['findings'] for b in bs)} findings, {sum(b['loc'] for b in bs)} LOC\n")
    sys.stderr.write(f"total {total} == census standing {census['standing_on_blankets']}; briefs -> {BRIEF_DIR}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
