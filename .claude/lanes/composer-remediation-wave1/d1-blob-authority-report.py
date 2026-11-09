import hashlib
import importlib
import json
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path('/workspace/elspeth/.claude/worktrees/composer-remediation-wave1')
LANE = Path('/workspace/elspeth/.claude/lanes/composer-remediation-wave1')
gate = importlib.import_module('tests.unit.architecture.test_session_db_mutation_authority')
j = json.loads((LANE / 'd1-blob-authority-identity-delta.json').read_text())
scopes = {label: {view: [gate.WriterIdentity(**site) for site in details[view]] for view in ('old', 'new')} for label, details in j['scopes'].items()}
probe = ROOT / 'd1_authority_control.py'
original_read = Path.read_bytes
def instrument(body):
    code = ('from elspeth.web.sessions.models import blobs_table\nfrom sqlalchemy import select\ndef control(conn):\n    ' + body + '\n').encode()
    with patch.object(Path, 'read_bytes', lambda path: code if path == probe else original_read(path)):
        return gate.scan_production_writers([probe], anchor=ROOT)
positive = instrument('conn.execute(blobs_table.delete())')
negative = instrument('conn.execute(select(blobs_table))')
assert [(s.table, s.operation) for s in positive] == [('blobs', 'delete')], positive
assert negative == [], negative
j['producer_controls'] = {'synthetic_delete': list(map(asdict, positive)), 'synthetic_select': list(map(asdict, negative))}

replacement = {'_RepositoryBlobMutations.'+name for name in ('prepare_blob_replacement','mark_blob_replacement_staged','commit_blob_replacement','retire_blob_replacement','abort_blob_replacement')}
storage = lambda s: s.table == 'output_storage_reservations' or s.symbol == '_RepositoryBlobMutations._set_run_output_storage'
tables = {'blobs','blob_deletion_cleanups','blob_run_links','blob_inline_resolutions','blob_replacement_cleanups'}
custody = lambda s: s.symbol == 'BlobServiceImpl._stage_output_blob_error_and_remove_bytes'
groups = {
 'blob replacement facet': ('repository', lambda s: s.symbol in replacement, gate._REVIEWED_WRITERS),
 'output storage facets': ('repository', storage, gate._REVIEWED_WRITERS),
 'blob service metadata writes': ('service_and_composer', lambda s: s.path.endswith('/blobs/service.py') and s.table in tables, gate._REVIEWED_WRITERS),
 'blob service contained acquisitions (assertion after first failure)': ('service_and_composer', lambda s: s.operation == 'write_connection' and s.authority == 'SessionBlobMutationAuthority', gate._REVIEWED_WRITERS),
 'atomic deletion retirement': ('repository', lambda s: s.symbol == '_RepositoryBlobMutations.retire_atomic_blob_deletion', gate._REVIEWED_WRITERS),
 'custody read admission (two failing tests share this baseline)': ('service', custody, gate._REVIEWED_READ_CONNECTIONS),
 'proposal effect receipts': ('proposal_receipts', lambda s: s.table == 'proposal_blob_effect_receipts', gate._REVIEWED_WRITERS),
}
lines = ['# D1 blob authority inventory attribution', '', 'Read-only diagnosis at base `ff755b12b5c01bcdc3e060a8f2d93b0219518de6`; production patch remained frozen. No production files, tests, policies, reviewed identities, signatures, or keys were changed/acquired.', '', '## Result', '', 'All seven reported failures are exact-identity line drift. The canonical producer detects no arrival, departure, duplication, changed writer fingerprint, changed authority, changed escaping-connection status, or changed path/symbol/table/operation/ordinal in the compared old/new source multisets. This is source-attribution proof, not admission or a passing gate.', '', 'Raw comparison log:', '', '```', (LANE/'d1-blob-authority-compare.log').read_text().strip(), '```', '', 'Complete old/new multisets, exact unexpected/stale identities, line pairings, and observed-source hashes are in `d1-blob-authority-identity-delta.json`. The scripts and log are lane-private evidence, never-stage.', '', '## Method and controls', '', '* Imported the unchanged gate module from the worktree and asserted both production package roots resolve to this worktree.', '* Called the actual `scan_production_writers` with the four distinct file scopes the failing tests use (repository alone; service+composer; service alone; repository+proposal effects+mutation capabilities). Also scanned the four remaining changed production files, which produced no writer identities in either source view.', '* Old source is `git show BASE:path` for each changed production file. Temporary in-process `Path.read_bytes` and `Path.read_text` overlays cover both the scanner input and imported-helper reads. Unchanged files are shared between views. No alternate regex/counting producer was used.', '* Compared complete `Counter` multisets using the real `_identity_key`; attribution removes only the `line` field using `dataclasses.replace(site, line=0)`. Identical-site negative control passes; line-only mutation fails the real identity comparison but passes attribution. Changed fingerprint, authority, ordinal, escape, and duplicate-count positive controls all fail attribution.', '* Canonical producer synthetic controls: exact `blobs/delete` is found; the same connection executing `select(blobs_table)` produces no writer. Controls patch only synthetic input reads in memory and leave files untouched.', '* All 13 source files observed by the producer retained identical SHA-256 values before/after measurement. Recheck retained source hashes before using this evidence on a later tree.', '', '## Failure-level evidence', '', '| Gate scope | Identities old/new | Exact unexpected/stale now | Only line differs |', '|---|---:|---:|---|']
for label, (scope, predicate, manifest) in groups.items():
    old = [s for s in scopes[scope]['old'] if predicate(s)]
    new = [s for s in scopes[scope]['new'] if predicate(s)]
    paths = {s.path for s in old+new}
    reviewed = [s for s in manifest if s.path in paths and predicate(s)]
    assert gate.inventory_drift(old, reviewed) == ([], []), label
    assert Counter(gate._identity_key(replace(s,line=0)) for s in old) == Counter(gate._identity_key(replace(s,line=0)) for s in new)
    unexpected, stale = gate.inventory_drift(new, reviewed)
    lines.append(f'| {label} | {len(old)}/{len(new)} | {len(unexpected)}/{len(stale)} | yes |')
lines += ['', 'The custody injection test fails on its unchanged-source baseline subtraction for its final case, before testing the final case’s injected mutation/escape. The earlier run is therefore not evidence that every injection case executed. It requires a fresh completed run after reviewed line re-derivation.', '', '## Exact refresh candidates', '', 'There are 101 distinct reviewed identities with moved source locations: 79 in `coordination/repository.py` and 22 in `blobs/service.py`. The service set contains 16 mutation/contained-acquisition entries and 6 reviewed read entries. All 80 repository producer sites move; only 79 are reviewed. The table below derives candidates by exact no-line identity join, not by editing signatures or guessing a line offset.', '', '| Manifest | Symbol | Table / operation | Fingerprint / ordinal | Old → new line |', '|---|---|---|---|---:|']
all_old = {gate._identity_key(s): s for scope in scopes.values() for s in scope['old']}
all_new = {gate._identity_key(s): s for scope in scopes.values() for s in scope['new']}
new_by_key = {}
for s in all_new.values():
    new_by_key.setdefault(gate._identity_key(replace(s,line=0)), []).append(s)
candidates = []
for name, manifest in [('_REVIEWED_WRITERS',gate._REVIEWED_WRITERS),('_REVIEWED_READ_CONNECTIONS',gate._REVIEWED_READ_CONNECTIONS),('_REVIEWED_NON_SESSION_CONNECTIONS',gate._REVIEWED_NON_SESSION_CONNECTIONS)]:
    for old in manifest:
        if gate._identity_key(old) not in all_old: continue
        matches = new_by_key[gate._identity_key(replace(old,line=0))]
        assert len(matches) == 1
        new = matches[0]
        if old.line != new.line:
            candidates.append({'manifest': name,'old':asdict(old),'new':asdict(new)})
            lines.append(f'| {name} | `{old.symbol}` | {old.table} / {old.operation} | `{old.fingerprint}` / {old.ordinal} | {old.line} → {new.line} |')
assert len(candidates) == 101
j['refresh_candidates'] = candidates
lines += ['', '## Existing debt stays excluded', '', 'The repository scope already emits one unreviewed escaping connection before D1: `_SessionOperationAuthorityRepository.__build_locked_fork_pair_controls.locked_pair_transaction`, `<sessions-write-connection>/write_connection`, fingerprint `749bb668e4cee8c8`, ordinal 1, authority `SessionOperationAuthority`, `connection_escape=True`, old line 4732 → new line 4851. Do not add it to any reviewed tuple. This unchanged baseline gap is distinct from the 101 already-reviewed line bindings.', '', '## Canonical review/remedy', '', '1. Keep the frozen production patch. Review its behavior separately; unchanged writer fingerprints do not prove unchanged prerequisites, quota behavior, locks, or runtime safety.', '2. Follow `CONTRIBUTING.md:587` (Sessions mutation-authority gate): re-derive the literal manifest entries with this gate’s `scan_production_writers` and review their complete identity diff. The manifest comment at gate line 1480 admits only writers already routed through exact table-specific typed authorities. No dedicated Sessions regeneration command was found in `scripts/`, `.agents/skills/`, or the searched maintainer/gate documentation; do not substitute Landscape’s `scripts/fencing_inventory.py`.', '3. In a separately authorized reviewed refresh, update only the 101 derived line fields for already-reviewed entries. Preserve fingerprints, ordinals, authority bindings, table policy, source shape, and `_identity_key`; do not admit the existing escape or widen a policy. Search other literal site assertions against the producer as well, since the reported selection covers only nine tests.', '4. Run the selected affected authority tests to completion with current worktree package provenance and frozen source hashes. Then run every affected Sessions authority gate that consumes the changed repository/service files; the 80/22 moved sites have broader reach than the seven reported assertion failures. Report existing whole-tree expected-failure debt separately. No gate is cleared by this diagnostic.', '5. HMAC trust-tier signing is a separate operator-controlled workflow. These Python writer tuples are not HMAC signatures; no signing-key request or signature manipulation is needed for their source-derived line refresh. Preserve any separate trust-tier review/signing obligations caused by the production patch.', '', '## Risks and limits', '', '* Complete producer equality except line is bounded to the changed production files and exact affected test scopes. It does not certify unrelated tree-wide inventories or runtime behavior.', '* Every repository identity moved because code was inserted above writer sites; different blocks have different line deltas. A global offset or source padding would be incorrect and would obscure honest source changes.', '* The existing 7-failed/1-passed/1-xfailed log remains red. This task did not alter expectations or rerun the heavy test selection.', '']
(LANE/'d1-blob-authority-identity-delta.json').write_text(json.dumps(j,indent=2)+'\n')
(LANE/'d1-blob-authority-inventory-analysis.md').write_text('\n'.join(lines))
print('producer controls: delete=1, select=0; refresh_candidates=',len(candidates))
print('source frozen recheck:', all(hashlib.sha256(Path(name).read_bytes()).hexdigest()==digest for name,digest in j['observed_source_sha256_before'].items()))
print('existing escape new:', next(s.line for s in scopes['repository']['new'] if s.fingerprint=='749bb668e4cee8c8'))
