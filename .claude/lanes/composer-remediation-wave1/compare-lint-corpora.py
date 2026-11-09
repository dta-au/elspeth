"""Lane-only diagnostic; compare semantic finding multisets, retaining locations."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

FIELDS = ('rule_id', 'file_path', 'severity', 'message', 'suggestion')


def normalize(rows, root):
    prefix = str(Path(root)) + '/'
    result = []
    for row in rows:
        if not isinstance(row, dict) or any(field not in row for field in FIELDS):
            raise ValueError('Unexpected finding record schema')
        copied = dict(row)
        name = copied['file_path']
        if not isinstance(name, str):
            raise ValueError('Expected path string')
        if name.startswith(prefix):
            copied['file_path'] = name[len(prefix):]
        result.append(copied)
    return result


def key(row):
    return tuple(json.dumps(row[field], sort_keys=True) for field in FIELDS)


def compare(before, after):
    left, right = Counter(map(key, before)), Counter(map(key, after))
    def expanded(counter):
        return [dict(zip(FIELDS, map(json.loads, item)), occurrences=count)
                for item, count in sorted(counter.items())]
    old_groups, new_groups = defaultdict(list), defaultdict(list)
    for row in before:
        old_groups[key(row)].append(row)
    for row in after:
        new_groups[key(row)].append(row)
    locations = []
    for item in sorted(left.keys() & right.keys()):
        old = Counter(json.dumps({f: row.get(f) for f in ('line', 'column', 'fingerprint')}, sort_keys=True)
                      for row in old_groups[item])
        new = Counter(json.dumps({f: row.get(f) for f in ('line', 'column', 'fingerprint')}, sort_keys=True)
                      for row in new_groups[item])
        if old != new:
            locations.append({'semantic': dict(zip(FIELDS, map(json.loads, item))),
                              'before': [{'location': json.loads(x), 'occurrences': n} for x, n in sorted(old.items())],
                              'after': [{'location': json.loads(x), 'occurrences': n} for x, n in sorted(new.items())]})
    return {'before_count': len(before), 'after_count': len(after),
            'compared_fields': FIELDS, 'normalized_only_checkout_prefix': True,
            'semantic_added_count': sum((right-left).values()),
            'semantic_removed_count': sum((left-right).values()),
            'semantic_added': expanded(right-left), 'semantic_removed': expanded(left-right),
            'location_changes': locations}


def controls():
    row = dict(rule_id='R', file_path='/old/src/a.py', severity='error', message='m', suggestion=None,
               line=1, column=2, fingerprint='a')
    left = normalize([row], '/old')
    moved = normalize([{**row, 'file_path': '/new/src/a.py', 'line': 9, 'fingerprint': 'b'}], '/new')
    same = compare(left, moved)
    assert same['semantic_added_count'] == same['semantic_removed_count'] == 0
    assert len(same['location_changes']) == 1
    assert compare(left, left+left)['semantic_added_count'] == 1
    assert compare(left+left, left)['semantic_removed_count'] == 1
    assert compare([], left)['semantic_added_count'] == 1
    assert compare(left, [])['semantic_removed_count'] == 1
    for field in FIELDS:
        altered = [{**left[0], field: 'changed'}]
        delta = compare(left, altered)
        assert delta['semantic_added_count'] == delta['semantic_removed_count'] == 1, field
    outside = normalize([{**row, 'file_path': '/old-other/a.py'}], '/old')
    assert outside[0]['file_path'] == '/old-other/a.py'
    try:
        normalize([{'file_path': '/old/x'}], '/old')
    except ValueError:
        pass
    else:
        raise AssertionError('Schema control must fail')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('before')
    parser.add_argument('after')
    parser.add_argument('output')
    parser.add_argument('--before-root', required=True)
    parser.add_argument('--after-root', required=True)
    args = parser.parse_args()
    controls()
    result = compare(normalize(json.loads(Path(args.before).read_text()), args.before_root),
                     normalize(json.loads(Path(args.after).read_text()), args.after_root))
    result['positive_negative_controls'] = 'passed, including duplicate multiplicity and each semantic field'
    Path(args.output).write_text(json.dumps(result, indent=2, sort_keys=True)+'\n')
    print(json.dumps({k: result[k] for k in ('before_count', 'after_count', 'semantic_added_count', 'semantic_removed_count', 'positive_negative_controls')}))
