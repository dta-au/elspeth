"""Compare every diagnostic field; permit only verified unchanged-line movement."""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

lane = Path('/workspace/elspeth/.claude/lanes/composer-remediation-wave1')
baseline = json.loads((lane / 'd2-boundaries-baseline.json').read_text())
candidate = json.loads(Path(sys.argv[1]).read_text())


def semantic(entry: dict[str, object]) -> str:
    return json.dumps({key: value for key, value in entry.items() if key != 'line'}, sort_keys=True)


def keys(entries: list[dict[str, object]]) -> Counter[str]:
    return Counter(semantic(entry) for entry in entries)


# A modified message must fail comparison; an unchanged clone must pass.
assert keys(baseline) == keys(json.loads(json.dumps(baseline)))
mutated = json.loads(json.dumps(baseline))
mutated[0]['message'] += ' NEGATIVE CONTROL'
assert keys(baseline) != keys(mutated)
added = keys(candidate) - keys(baseline)
removed = keys(baseline) - keys(candidate)
before_groups: dict[str, list[dict[str, object]]] = defaultdict(list)
after_groups: dict[str, list[dict[str, object]]] = defaultdict(list)
for entry in baseline:
    before_groups[semantic(entry)].append(entry)
for entry in candidate:
    after_groups[semantic(entry)].append(entry)
line_maps: dict[str, dict[int, int]] = {}
line_offsets = []
position_errors = []
for key in before_groups.keys() & after_groups.keys():
    before = sorted(before_groups[key], key=lambda entry: entry['line'])
    after = sorted(after_groups[key], key=lambda entry: entry['line'])
    for old, new in zip(before, after, strict=False):
        name = str(old['file_path'])
        if name not in line_maps:
            relative = 'src/elspeth/' + name
            original = subprocess.check_output(['git', 'show', 'HEAD:' + relative], text=True).splitlines()
            current = Path(relative).read_text().splitlines()
            mapping = {}
            for block in SequenceMatcher(None, original, current, autojunk=False).get_matching_blocks():
                for offset in range(block.size):
                    mapping[block.a + offset + 1] = block.b + offset + 1
            line_maps[name] = mapping
        expected = line_maps[name].get(int(old['line']))
        if expected != new['line']:
            position_errors.append({'file': name, 'old': old['line'], 'new': new['line'], 'mapped': expected})
        elif old['line'] != new['line']:
            line_offsets.append({'file': name, 'old': old['line'], 'new': new['line']})
report = {
    'baseline_findings': len(baseline), 'candidate_findings': len(candidate),
    'semantic_added': [json.loads(key) for key in added.elements()],
    'semantic_removed': [json.loads(key) for key in removed.elements()],
    'position_errors': position_errors, 'verified_unchanged_line_movements': line_offsets,
    'positive_clone_control': 'passed', 'negative_message_control': 'passed',
}
(lane / 'd2-checks/boundary-delta.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps({key: value for key, value in report.items() if key != 'verified_unchanged_line_movements'}, indent=2))
sys.exit(1 if added or removed or position_errors else 0)
