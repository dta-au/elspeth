"""R1 focused gates, exact receipts and complete source/test freeze."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

root = Path.cwd()
lane = Path('/workspace/elspeth/.claude/lanes/composer-remediation-wave1')
out = lane / 'd2-fix1'
python = str(root / '.venv/bin/python')
env = dict(os.environ)
env['PYTHONPATH'] = f'{root}/src:{root}/elspeth-lints/src'
env['MYPYPATH'] = f'{root}/src:{root}/elspeth-lints/src:{root}/typings'
env['ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE'] = 'shape-only-when-key-missing'
subprocess.run([python, '-c', 'import elspeth, elspeth_lints; print(elspeth.__file__); print(elspeth_lints.__file__)'], env=env, check=True)
subprocess.run([python, str(lane / 'd2-checks/freeze_tree.py'), str(out / 'before.json')], env=env, check=True)
production = ['src/elspeth/web/execution/_validation_authoring.py', 'src/elspeth/web/execution/preflight.py']
changed = production + ['tests/unit/composer_mcp/test_configured_root_admission.py', 'tests/unit/web/execution/test_validation_authoring.py', 'tests/unit/web/execution/test_preflight_side_effects.py']
tests = [
    'tests/unit/composer_mcp', 'tests/unit/web/test_paths.py',
    'tests/unit/web/execution/test_validation_authoring.py',
    'tests/unit/web/execution/test_validation_materialization.py',
    'tests/unit/web/execution/test_validation_identity_document.py',
    'tests/unit/web/execution/test_validation.py',
    'tests/unit/web/execution/test_preflight_side_effects.py',
    'tests/unit/web/execution/test_service.py::TestResolveYamlPaths',
    'tests/unit/contracts/test_leaf_boundary.py',
    'tests/unit/web/test_sessions_composer_attribute_contracts.py',
]
stages = [
    ('ruff', [str(root / '.venv/bin/ruff'), 'check', *changed]),
    ('format', [str(root / '.venv/bin/ruff'), 'format', '--check', *changed]),
    ('mypy', [str(root / '.venv/bin/mypy'), '--cache-dir', str(out / 'mypy-cache'), *production]),
    ('contracts', [python, '-m', 'scripts.check_contracts']),
    ('boundaries', [python, '-m', 'elspeth_lints.core.cli', 'check', '--rules', 'trust_boundary.tests,trust_boundary.scope,trust_boundary.tier', '--root', 'src/elspeth', '--format', 'json']),
    ('pytest', [python, '-m', 'pytest', '-o', f'pythonpath={root}/src {root}/elspeth-lints/src', '--basetemp=' + str(out / 'final-tmp'), '-n', '0', *tests]),
]
receipts = []
for name, command in stages:
    print(f'START {name}', flush=True)
    with (out / (name + ('.json' if name == 'boundaries' else '.log'))).open('w') as log:
        if name == 'boundaries':
            with (out / 'boundaries.stderr').open('w') as errors:
                result = subprocess.run(command, env=env, stdout=log, stderr=errors)
        else:
            result = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT)
    (out / f'{name}.exit').write_text(str(result.returncode) + '\n')
    receipts.append({'stage': name, 'command': command, 'exit': result.returncode})
    (out / 'receipts.json').write_text(json.dumps(receipts, indent=2) + '\n')
    print(f'EXIT {name} {result.returncode}', flush=True)
subprocess.run([python, str(lane / 'd2-checks/freeze_tree.py'), str(out / 'after.json')], env=env, check=True)
frozen = (out / 'before.json').read_bytes() == (out / 'after.json').read_bytes()
(out / 'frozen.txt').write_text(f'frozen={frozen}\n')
print(f'FROZEN {frozen}', flush=True)
sys.exit(0 if frozen and all(receipt['exit'] == 0 for receipt in receipts) else 1)
