from pathlib import Path
import hashlib
import json
import os
import subprocess

LANE = Path(__file__).parent
ROOT = Path('/workspace/elspeth/.claude/worktrees/composer-remediation-landscape-metadata')
PREFIX = LANE / 'combined-chroma-plugin-smoke'
PLUGIN_ROOT = LANE / 'runtime-deps/pytest_plugins'


def snapshot():
    assert not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT)
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    assert head == 'f9c23c59c63bbd3099029e995a778bade594f6c8'
    files = {}
    for raw in subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).split(b'\0'):
        if not raw:
            continue
        name = os.fsdecode(raw)
        path = ROOT / name
        if path.is_symlink():
            files[name] = {'target': os.readlink(path)}
        else:
            with path.open('rb') as stream:
                files[name] = {'sha256': hashlib.file_digest(stream, 'sha256').hexdigest()}
    files['runtime-plugin'] = {'sha256': hashlib.sha256((PLUGIN_ROOT / 'composer_test_cache.py').read_bytes()).hexdigest()}
    return {'head': head, 'files': files}


before = snapshot()
PREFIX.with_suffix('.before.json').write_text(json.dumps(before, sort_keys=True))
env = dict(os.environ)
env['PYTHONPATH'] = f'{ROOT}/src:{ROOT}/elspeth-lints/src'
cmd = [str(ROOT / '.venv/bin/python'), '-m', 'pytest', '-n', '1', '-o', f'pythonpath={ROOT}/src {ROOT}/elspeth-lints/src', '-p', 'composer_test_cache', '-o', f'pythonpath={ROOT}/src {ROOT}/elspeth-lints/src {PLUGIN_ROOT}', 'tests/unit/plugins/infrastructure/clients/retrieval/test_chroma.py::TestChromaSearchProvider::test_search_returns_retrieval_chunks', 'tests/integration/pipeline/test_rag_indexed_smoke.py::TestRAGIndexedSmoke::test_full_pipeline_sequence']
PREFIX.with_suffix('.command.json').write_text(json.dumps({'cwd': str(ROOT), 'argv': cmd, 'PYTHONPATH': env['PYTHONPATH']}, indent=2))
with PREFIX.with_suffix('.log').open('w') as log:
    result = subprocess.run(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
PREFIX.with_suffix('.exit').write_text(f'{result.returncode}\n')
after = snapshot()
PREFIX.with_suffix('.after.json').write_text(json.dumps(after, sort_keys=True))
PREFIX.with_suffix('.frozen-exit').write_text('0\n' if before == after else '1\n')
print(json.dumps({'exit': result.returncode, 'frozen': before == after}))
print('\n'.join(PREFIX.with_suffix('.log').read_text().splitlines()[-22:]))
assert before == after
raise SystemExit(result.returncode)
