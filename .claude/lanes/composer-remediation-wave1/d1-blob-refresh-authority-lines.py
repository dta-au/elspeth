"""Regenerate only already-reviewed source locations from the real producer."""
from __future__ import annotations

import ast
import difflib
import hashlib
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from tests.unit.architecture import test_session_db_mutation_authority as gate

root = Path.cwd()
lane = Path('/workspace/elspeth/.claude/lanes/composer-remediation-wave1')
analysis = json.loads((lane / 'd1-blob-authority-identity-delta.json').read_text())
repo = 'src/elspeth/web/coordination/repository.py'
service = 'src/elspeth/web/blobs/service.py'
composer = 'src/elspeth/web/composer/tools/blobs.py'
for name in (repo, service):
    path = root / name
    assert hashlib.sha256(path.read_bytes()).hexdigest() == analysis['observed_source_sha256_before'][str(path)]

def key(row):
    return tuple((name, value) for name, value in row.items() if name != 'line')

produced = []
for paths in ((repo,), (service, composer)):
    produced.extend(asdict(row) for row in gate.scan_production_writers([root / path for path in paths], anchor=root))
by_key = {key(row): row for row in produced}
assert len(by_key) == len(produced)
candidates = analysis['refresh_candidates']
assert len(candidates) == 101
for candidate in candidates:
    assert key(candidate['old']) == key(candidate['new'])
    assert by_key[key(candidate['old'])] == candidate['new']

target = root / 'tests/unit/architecture/test_session_db_mutation_authority.py'
source = target.read_text()
tree = ast.parse(source)
lines = source.splitlines(keepends=True)
edits = []
seen = Counter()
for assignment in tree.body:
    if not isinstance(assignment, ast.AnnAssign) or not isinstance(assignment.target, ast.Name):
        continue
    manifest = assignment.target.id
    if manifest not in {'_REVIEWED_WRITERS', '_REVIEWED_READ_CONNECTIONS'}:
        continue
    for node in ast.walk(assignment.value):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != 'WriterIdentity':
            continue
        row = asdict(gate.WriterIdentity(*[ast.literal_eval(value) for value in node.args], **{keyword.arg: ast.literal_eval(keyword.value) for keyword in node.keywords}))
        for candidate in candidates:
            if candidate['manifest'] == manifest and key(candidate['old']) == key(row):
                assert row == candidate['old']
                keyword = next(keyword for keyword in node.keywords if keyword.arg == 'line')
                value = keyword.value
                assert isinstance(value, ast.Constant) and type(value.value) is int
                start = sum(map(len, lines[:value.lineno - 1])) + value.col_offset
                end = sum(map(len, lines[:value.end_lineno - 1])) + value.end_col_offset
                assert source[start:end] == str(row['line'])
                edits.append((start, end, str(candidate['new']['line'])))
                seen[(manifest, key(row))] += 1
assert len(edits) == 101
assert set(seen.values()) == {1}
regenerated = source
for start, end, value in sorted(edits, reverse=True):
    regenerated = regenerated[:start] + value + regenerated[end:]

def without_line_literals(text):
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'WriterIdentity':
            for keyword in node.keywords:
                if keyword.arg == 'line':
                    keyword.value = ast.Constant(0)
    return ast.dump(tree, include_attributes=False)

assert without_line_literals(source) == without_line_literals(regenerated)
diff = list(difflib.unified_diff(source.splitlines(keepends=True), regenerated.splitlines(keepends=True), n=3))
patch = '*** Begin Patch\n*** Update File: ' + str(target) + '\n' + ''.join('@@\n' if line.startswith('@@ ') else line for line in diff[2:]) + '*** End Patch\n'
(lane / 'd1-blob-authority-lines.patch').write_text(patch)
(lane / 'd1-blob-authority-lines-produced.json').write_text(json.dumps(produced, indent=2) + '\n')
print('Produced 101 line-only refreshes; all non-line AST and identities unchanged; no source files edited.')
