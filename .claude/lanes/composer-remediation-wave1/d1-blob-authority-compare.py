"""Read-only attribution: canonical Sessions scanner over frozen source views."""
import hashlib
import importlib
import json
import subprocess
import time
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path('/workspace/elspeth/.claude/worktrees/composer-remediation-wave1')
LANE = Path('/workspace/elspeth/.claude/lanes/composer-remediation-wave1')
BASE = 'ff755b12b5c01bcdc3e060a8f2d93b0219518de6'
gate = importlib.import_module('tests.unit.architecture.test_session_db_mutation_authority')
import elspeth
import elspeth_lints
assert ROOT in Path(elspeth.__file__).resolve().parents
assert ROOT in Path(elspeth_lints.__file__).resolve().parents
assert Path(gate.__file__).resolve() == ROOT / 'tests/unit/architecture/test_session_db_mutation_authority.py'
changed = subprocess.check_output(['git', 'diff', '--name-only', BASE, '--', 'src'], cwd=ROOT, text=True).splitlines()
old_bytes = {ROOT / name: subprocess.check_output(['git', 'show', f'{BASE}:{name}'], cwd=ROOT) for name in changed}
read_bytes = Path.read_bytes
read_text = Path.read_text
observed = {}
def frozen_read_bytes(path):
    data = read_bytes(path)
    resolved = path.resolve()
    if ROOT in resolved.parents and resolved.suffix == '.py':
        observed.setdefault(str(resolved), hashlib.sha256(data).hexdigest())
    return data
def frozen_read_text(path, *args, **kwargs):
    frozen_read_bytes(path)
    return read_text(path, *args, **kwargs)
def old_read_bytes(path):
    resolved = path.resolve()
    frozen_read_bytes(path)
    return old_bytes[resolved] if resolved in old_bytes else read_bytes(path)
def old_read_text(path, *args, **kwargs):
    resolved = path.resolve()
    frozen_read_bytes(path)
    return old_bytes[resolved].decode(kwargs.get('encoding') or 'utf-8') if resolved in old_bytes else read_text(path, *args, **kwargs)

repo = 'src/elspeth/web/coordination/repository.py'
service = 'src/elspeth/web/blobs/service.py'
composer = 'src/elspeth/web/composer/tools/blobs.py'
scopes = {
    'repository': [repo],
    'service_and_composer': [service, composer],
    'service': [service],
    'proposal_receipts': [repo, 'src/elspeth/web/sessions/proposal_blob_effects.py', 'src/elspeth/web/sessions/mutation_capabilities.py'],
    'other_changed_production': [name for name in changed if name not in {repo, service, composer}],
}
def no_line(site):
    return gate._identity_key(replace(site, line=0))
def delta(old, new):
    unexpected, stale = gate.inventory_drift(new, old)
    old_counter, new_counter = Counter(map(no_line, old)), Counter(map(no_line, new))
    pairs = []
    if old_counter == new_counter:
        for key in sorted(set(old_counter), key=repr):
            before = sorted((site for site in old if no_line(site) == key), key=lambda site: site.line)
            after = sorted((site for site in new if no_line(site) == key), key=lambda site: site.line)
            pairs.extend({'old': asdict(a), 'new': asdict(b)} for a, b in zip(before, after, strict=True) if a != b)
    return {'old_count': len(old), 'new_count': len(new), 'exact_unexpected': list(map(asdict, unexpected)), 'exact_stale': list(map(asdict, stale)), 'equal_except_line': old_counter == new_counter, 'line_moves': pairs}

results = {'base': BASE, 'changed_production': changed, 'provenance': {'gate': gate.__file__, 'elspeth': elspeth.__file__, 'elspeth_lints': elspeth_lints.__file__}, 'scopes': {}}
raw = {}
for label, paths in scopes.items():
    start = time.monotonic()
    with patch.object(Path, 'read_bytes', old_read_bytes), patch.object(Path, 'read_text', old_read_text):
        old = gate.scan_production_writers([ROOT / name for name in paths], anchor=ROOT)
    with patch.object(Path, 'read_bytes', frozen_read_bytes), patch.object(Path, 'read_text', frozen_read_text):
        new = gate.scan_production_writers([ROOT / name for name in paths], anchor=ROOT)
    raw[label] = old, new
    reviewed = [site for site in (*gate._REVIEWED_WRITERS, *gate._REVIEWED_READ_CONNECTIONS, *gate._REVIEWED_NON_SESSION_CONNECTIONS) if site.path in paths]
    results['scopes'][label] = {'paths': paths, 'old': list(map(asdict, old)), 'new': list(map(asdict, new)), 'delta': delta(old, new), 'old_vs_reviewed': delta(reviewed, old), 'new_vs_reviewed': delta(reviewed, new)}
    print(label, 'old', len(old), 'new', len(new), 'equal_except_line', Counter(map(no_line, old)) == Counter(map(no_line, new)), 'seconds', round(time.monotonic()-start, 2), flush=True)

# Positive and negative controls exercise the exact producer/key and attribution
# comparator without writing production files or replacing reviewed expectations.
sample = next(site for site in raw['repository'][1] if site.table == 'blobs')
assert gate.inventory_drift([sample], [sample]) == ([], [])
shifted = replace(sample, line=sample.line+1)
assert gate.inventory_drift([shifted], [sample]) == ([shifted], [sample])
assert delta([sample], [shifted])['equal_except_line']
for field, value in [('fingerprint', '0'*16), ('authority', 'WrongAuthority'), ('ordinal', sample.ordinal+1), ('connection_escape', not sample.connection_escape)]:
    mutated = replace(sample, **{field: value})
    assert not delta([sample], [mutated])['equal_except_line']
assert not delta([sample], [sample, sample])['equal_except_line']
results['controls'] = {'unchanged_exact_match': True, 'line_drift_rejected_exact_but_classified_line_only': True, 'fingerprint_authority_ordinal_escape_and_duplicate_rejected': True}
results['observed_source_sha256_before'] = observed
results['observed_sources_frozen'] = all(hashlib.sha256(read_bytes(Path(name))).hexdigest() == digest for name, digest in observed.items())
assert results['observed_sources_frozen']
(LANE / 'd1-blob-authority-identity-delta.json').write_text(json.dumps(results, indent=2)+'\n')
print('observed_sources_frozen', len(observed), results['observed_sources_frozen'], flush=True)
