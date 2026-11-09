"""Normalize only proven source line moves and their derived diagnostic IDs."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

lane = Path('/workspace/elspeth/.claude/lanes/composer-remediation-wave1')
baseline = json.loads((lane / 'd2-boundaries-baseline.json').read_text())
candidate = json.loads((lane / 'd2-checks/post-contract-boundaries.json').read_text())


def identity(entry):
    return json.dumps({key: value for key, value in entry.items() if key not in {'line', 'fingerprint'}}, sort_keys=True)


def derived_fingerprint(entry):
    # Exact shared.py::_make_finding payload, checked for BOTH recorded sets.
    payload = f"{entry['rule_id']}|{entry['file_path']}|{entry['line']}|{entry['column']}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def compare(after):
    errors = []
    for label, entries in [('baseline', baseline), ('candidate', after)]:
        for entry in entries:
            if derived_fingerprint(entry) != entry['fingerprint']:
                errors.append({'kind': 'fingerprint-derivation', 'set': label, 'entry': entry})
    before_groups, after_groups = defaultdict(list), defaultdict(list)
    for entry in baseline:
        before_groups[identity(entry)].append(entry)
    for entry in after:
        after_groups[identity(entry)].append(entry)
    left, right = Counter(map(identity, baseline)), Counter(map(identity, after))
    added, removed = list((right - left).elements()), list((left - right).elements())
    movements, maps = [], {}
    for key in before_groups.keys() & after_groups.keys():
        for old, new in zip(sorted(before_groups[key], key=lambda item: item['line']), sorted(after_groups[key], key=lambda item: item['line'])):
            name = old['file_path']
            if name not in maps:
                relative = 'src/elspeth/' + name
                original = subprocess.check_output(['git', 'show', 'HEAD:' + relative], text=True).splitlines()
                current = Path(relative).read_text().splitlines()
                maps[name] = {}
                for block in SequenceMatcher(None, original, current, autojunk=False).get_matching_blocks():
                    maps[name].update((block.a + i + 1, block.b + i + 1) for i in range(block.size))
            expected = maps[name].get(old['line'])
            if expected != new['line']:
                errors.append({'kind': 'source-line-mapping', 'old': old, 'new': new, 'expected_line': expected})
            elif old['line'] != new['line']:
                movements.append({'file': name, 'old_line': old['line'], 'new_line': new['line'],
                                  'old_fingerprint': old['fingerprint'], 'new_fingerprint': new['fingerprint'],
                                  'message': old['message']})
    return {'added': added, 'removed': removed, 'errors': errors, 'verified_location_changes': movements}


result = compare(candidate)
bad_message = json.loads(json.dumps(candidate))
bad_message[0]['message'] += ' NEGATIVE CONTROL'
assert compare(bad_message)['added']
bad_id = json.loads(json.dumps(candidate))
bad_id[0]['fingerprint'] = '0' * 16
assert compare(bad_id)['errors']
bad_line = json.loads(json.dumps(candidate))
bad_line[0]['line'] += 1
bad_line[0]['fingerprint'] = derived_fingerprint(bad_line[0])
assert compare(bad_line)['errors']
result.update({'baseline_findings': len(baseline), 'candidate_findings': len(candidate),
               'message_mutation_control': 'passed', 'fingerprint_mutation_control': 'passed', 'line_mutation_control': 'passed'})
(lane / 'd2-checks/boundary-location-delta.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
sys.exit(1 if result['added'] or result['removed'] or result['errors'] else 0)
