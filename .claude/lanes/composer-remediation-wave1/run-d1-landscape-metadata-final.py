"""Capture focused verification with complete tracked/untracked source freeze."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

LANE = Path(__file__).parent
ROOT = Path('/workspace/elspeth/.claude/worktrees/composer-remediation-landscape-metadata')
PREFIX = LANE / 'd1-landscape-metadata-final'


def snapshot():
    files = {}
    paths = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT).split(b'\0')
    for raw in paths:
        if not raw:
            continue
        name = os.fsdecode(raw)
        path = ROOT / name
        if path.is_symlink():
            files[name] = {'target': os.readlink(path)}
        else:
            with path.open('rb') as source:
                files[name] = {'sha256': hashlib.file_digest(source, 'sha256').hexdigest()}
    return {
        'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'status': subprocess.check_output(['git', '--no-optional-locks', 'status', '--porcelain', '-uall'], cwd=ROOT, text=True),
        'files': files,
    }


before = snapshot()
PREFIX.with_suffix('.before.json').write_text(json.dumps(before, sort_keys=True))
env = dict(os.environ)
env['PYTHONPATH'] = f'{ROOT}/src:{ROOT}/elspeth-lints/src'
targets = [
    'tests/testcontainer/web/test_schema_probe_postgres.py::test_landscape_database_construction_stamps_postgres_identity',
    'tests/testcontainer/web/test_schema_probe_postgres.py::test_postgres_landscape_creation_preserves_later_sqlite_schema',
    'tests/testcontainer/web/test_blob_mutation_admission_postgres.py',
    'tests/testcontainer/web/test_schema_probe_postgres.py::test_fresh_create_reaches_current',
    'tests/testcontainer/web/test_schema_probe_postgres.py::test_postgres_schema_identity_drift_is_stale_and_not_repaired',
    'tests/testcontainer/web/test_schema_probe_postgres.py::test_concurrent_initializers_serialize_to_one_create_all',
    'tests/unit/core/landscape/test_database_metadata_creation.py',
    'tests/unit/core/landscape/test_database_compatibility_guards.py',
    'tests/unit/core/landscape/test_database_schema_probe.py',
    'tests/unit/core/landscape/test_database_engine_lifecycle.py',
    'tests/unit/core/landscape/test_database_pragma_probe.py',
    'tests/unit/architecture/test_web_landscape_mutation_fencing.py::test_new_landscape_stamp_requires_exact_locked_empty_target_sources',
    'tests/unit/architecture/test_web_landscape_mutation_fencing.py::test_new_landscape_stamp_refuses_incomplete_or_foreign_authority',
    'tests/unit/architecture/test_session_db_mutation_authority.py::test_non_session_connection_manifest_is_bidirectional_and_exact',
    'tests/unit/architecture/test_session_db_mutation_authority.py::test_writer_manifest_is_bidirectional_and_multiplicity_aware',
]
cmd = [str(ROOT / '.venv/bin/python'), '-m', 'pytest', '-n', '0', '-m', 'testcontainer or not testcontainer', '-o', f'pythonpath={ROOT}/src {ROOT}/elspeth-lints/src', *targets]
PREFIX.with_suffix('.command.json').write_text(json.dumps({'cwd': str(ROOT), 'argv': cmd, 'PYTHONPATH': env['PYTHONPATH']}, indent=2))
with PREFIX.with_suffix('.log').open('w') as log:
    result = subprocess.run(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
PREFIX.with_suffix('.exit').write_text(f'{result.returncode}\n')
after = snapshot()
PREFIX.with_suffix('.after.json').write_text(json.dumps(after, sort_keys=True))
frozen = before == after
PREFIX.with_suffix('.frozen-exit').write_text('0\n' if frozen else '1\n')
print(json.dumps({'exit': result.returncode, 'frozen': frozen, 'files': len(before['files'])}))
assert frozen
raise SystemExit(result.returncode)
