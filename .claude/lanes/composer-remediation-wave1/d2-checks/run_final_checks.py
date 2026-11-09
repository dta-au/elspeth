"""Serial D2 scoped verification with exact receipts and frozen inputs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

root = Path.cwd()
lane = Path('/workspace/elspeth/.claude/lanes/composer-remediation-wave1/d2-checks')
python = str(root / '.venv/bin/python')
env = dict(os.environ)
env['PYTHONPATH'] = f'{root}/src:{root}/elspeth-lints/src'
env['MYPYPATH'] = f'{root}/src:{root}/elspeth-lints/src:{root}/typings'
env['ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE'] = 'shape-only-when-key-missing'
subprocess.run([python, '-c', 'import elspeth, elspeth_lints; print(elspeth.__file__); print(elspeth_lints.__file__)'], env=env, check=True)
subprocess.run([python, str(lane / 'freeze_tree.py'), str(lane / 'final-before.json')], env=env, check=True)
changed = subprocess.check_output(['git', 'diff', '--name-only', '--', '*.py'], text=True).splitlines()
changed.append('tests/unit/composer_mcp/test_configured_root_admission.py')
production = [name for name in changed if name.startswith('src/')]
tests = [
    'tests/unit/composer_mcp',
    'tests/unit/web/test_paths.py',
    'tests/unit/web/composer/test_set_pipeline_candidate.py',
    'tests/unit/web/composer/test_source_rejection_contracts.py',
    'tests/unit/web/composer/test_output_rejection_contracts.py',
    'tests/unit/web/composer/test_transform_rejection_contracts.py',
    'tests/unit/web/composer/test_patch_source_options_noop.py',
    'tests/unit/web/composer/test_splice_transform_tool.py',
    'tests/unit/web/execution/test_validation_authoring.py',
    'tests/unit/web/execution/test_validation_materialization.py',
    'tests/unit/web/execution/test_validation_identity_document.py',
    'tests/unit/web/execution/test_validation.py',
    'tests/unit/web/execution/test_preflight_side_effects.py',
    'tests/unit/web/execution/test_service.py::TestResolveYamlPaths',
    'tests/unit/web/test_sessions_composer_attribute_contracts.py',
    'tests/unit/elspeth_lints/test_masquerade_gate.py',
]
stages = [
    ('ruff', [str(root / '.venv/bin/ruff'), 'check', *changed]),
    ('format', [str(root / '.venv/bin/ruff'), 'format', '--check', *changed]),
    ('mypy', [str(root / '.venv/bin/mypy'), '--cache-dir', str(lane / 'mypy-cache'), *production]),
    ('contracts', [python, '-m', 'scripts.check_contracts']),
    ('inventory', [python, 'scripts/cicd/generate_skill_inventory.py', '--check']),
    ('boundaries', [python, '-m', 'elspeth_lints.core.cli', 'check', '--rules', 'trust_boundary.tests,trust_boundary.scope,trust_boundary.tier', '--root', 'src/elspeth']),
    ('pytest', [python, '-m', 'pytest', '-o', f'pythonpath={root}/src {root}/elspeth-lints/src', '--basetemp=' + str(lane / 'final-tmp'), '-n', '0', *tests]),
]
receipts = []
for name, command in stages:
    print(f'START {name}', flush=True)
    with (lane / f'final-{name}.log').open('w') as log:
        result = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT)
    (lane / f'final-{name}.exit').write_text(str(result.returncode) + '\n')
    receipts.append({'stage': name, 'command': command, 'exit': result.returncode})
    (lane / 'final-receipts.json').write_text(json.dumps(receipts, indent=2) + '\n')
    print(f'EXIT {name} {result.returncode}', flush=True)
subprocess.run([python, str(lane / 'freeze_tree.py'), str(lane / 'final-after.json')], env=env, check=True)
frozen = (lane / 'final-before.json').read_bytes() == (lane / 'final-after.json').read_bytes()
(lane / 'final-frozen.txt').write_text(f'frozen={frozen}\n')
print(f'FROZEN {frozen}', flush=True)
sys.exit(0 if frozen and all(receipt['exit'] == 0 for receipt in receipts) else 1)
