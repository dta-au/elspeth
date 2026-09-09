"""Census of tier-model findings that stand on ``per_file_rules`` blankets.

Run from the repository root, key-free::

    ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
      PYTHONPATH=elspeth-lints/src python \
      docs/plans/2026-08-30-per-file-blanket-migration-tools/blanket_census.py > census.json

Prints, per blanket (allowlist file, pattern, rules, max_hits), the findings
that would SURFACE if that blanket were deleted today: every raw finding the
blanket matches that no exact ``allow_hits`` entry also covers. The same
matcher the production rule uses (``_match_per_file_rule``) decides
membership, so the census cannot drift from the gate. Also prints a
``by_file`` roll-up (file -> rule -> count) and ``unused`` blankets (zero
findings), which are dead and can be deleted with no other work.

Whole-tree raw scan; takes ~1 minute.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from elspeth_lints.core.tier_model_scan import scan_tree_findings
from elspeth_lints.rules.trust_tier.tier_model.rotate import _finding_key_for
from elspeth_lints.rules.trust_tier.tier_model.rule import _load_tier_model_allowlist, _match_per_file_rule

ROOT = Path("src/elspeth")
ALLOWLIST_DIR = Path("config/cicd/enforce_tier_model")


def _canonical_key(finding: object) -> str:
    key = finding.canonical_key
    return key() if callable(key) else key


def main() -> int:
    allowlist = _load_tier_model_allowlist(ALLOWLIST_DIR, source_root=ROOT)
    exact_keys = {entry.key for entry in allowlist.entries}
    findings = scan_tree_findings(root=ROOT)

    by_blanket: dict[str, list[str]] = defaultdict(list)
    by_file: dict[str, Counter[str]] = defaultdict(Counter)
    for finding in findings:
        if _canonical_key(finding) in exact_keys:
            continue
        rule = _match_per_file_rule(allowlist.per_file_rules, _finding_key_for(finding))
        if rule is None:
            continue
        blanket_id = f"{rule.source_file}::{rule.pattern}::{','.join(rule.rules)}::max_hits={rule.max_hits}"
        by_blanket[blanket_id].append(f"{finding.file_path}:{finding.line}:{finding.rule_id}")
        by_file[str(finding.file_path)][finding.rule_id] += 1

    unused = [
        f"{rule.source_file}::{rule.pattern}::{','.join(rule.rules)}::max_hits={rule.max_hits}"
        for rule in allowlist.per_file_rules
        if not by_blanket.get(f"{rule.source_file}::{rule.pattern}::{','.join(rule.rules)}::max_hits={rule.max_hits}")
    ]
    report = {
        "raw_findings": len(findings),
        "blanket_rules": len(allowlist.per_file_rules),
        "standing_on_blankets": sum(len(v) for v in by_blanket.values()),
        "unused_blankets": unused,
        "by_blanket": {k: sorted(v) for k, v in sorted(by_blanket.items())},
        "by_file": {f: dict(sorted(c.items())) for f, c in sorted(by_file.items())},
    }
    json.dump(report, sys.stdout, indent=1)
    sys.stdout.write("\n")
    sys.stderr.write(
        f"raw={report['raw_findings']} blankets={report['blanket_rules']} standing={report['standing_on_blankets']} unused={len(unused)}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
