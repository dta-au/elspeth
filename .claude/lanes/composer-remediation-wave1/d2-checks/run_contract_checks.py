"""Verify the contracts placement correction on a fresh complete input freeze."""

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
subprocess.run([python, str(lane / 'freeze_tree.py'), str(lane / 'post-contract-before.json')], env=env, check=True)
changed = subprocess.check_output(['git', 'diff', '--name-only', '--', '*.py'], text=True).splitlines()
changed.extend(['tests/unit/composer_mcp/test_configured_root_admission.py', 'src/elspeth/contracts/composer_paths.py'])
production = [name for name in changed if name.startswith('src/')]
tests = [
    'tests/unit/composer_mcp',
    'tests/unit/web/test_paths.py',
    'tests/unit/web/composer/test_set_pipeline_candidate.py::test_later_path_refusal_does_not_persist_a_prepared_inline_source',
    'tests/unit/web/execution/test_validation_authoring.py',
    'tests/unit/web/execution/test_validation_materialization.py',
    'tests/unit/web/execution/test_validation_identity_document.py',
    'tests/unit/web/execution/test_validation.py::test_validate_pipeline_public_signature_is_stable_through_boundary_decorator',
    'tests/unit/web/execution/test_preflight_side_effects.py',
    'tests/unit/web/execution/test_service.py::TestResolveYamlPaths',
    'tests/unit/contracts/test_leaf_boundary.py',
    'tests/unit/web/test_sessions_composer_attribute_contracts.py',
]
stages = [
    ('ruff', [str(root / '.venv/bin/ruff'), 'check', *changed]),
    ('format', [str(root / '.venv/bin/ruff'), 'format', '--check', *changed]),
    ('mypy', [str(root / '.venv/bin/mypy'), '--cache-dir', str(lane / 'mypy-cache'), *production]),
    ('contracts', [python, '-m', 'scripts.check_contracts']),
    ('inventory', [python, 'scripts/cicd/generate_skill_inventory.py', '--check']),
    ('boundaries', [python, '-m', 'elspeth_lints.core.cli', 'check', '--rules', 'trust_boundary.tests,trust_boundary.scope,trust_boundary.tier', '--root', 'src/elspeth', '--format', 'json']),
    ('boundary-delta', [python, str(lane / 'compare_boundaries.py'), str(lane / 'post-contract-boundaries.json')]),
    ('pytest', [python, '-m', 'pytest', '-o', f'pythonpath={root}/src {root}/elspeth-lints/src', '--basetemp=' + str(lane / 'post-contract-tmp'), '-n', '0', *tests]),
]
receipts = []
for name, command in stages:
    print(f'START {name}', flush=True)
    suffix = 'json' if name == 'boundaries' else 'log'
    with (lane / f'post-contract-{name}.{suffix}').open('w') as log:
        if name == 'boundaries':
            with (lane / 'post-contract-boundaries.stderr').open('w') as errors:
                result = subprocess.run(command, env=env, stdout=log, stderr=errors)
        else:
            result = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT)
    (lane / f'post-contract-{name}.exit').write_text(str(result.returncode) + '\n')
    receipts.append({'stage': name, 'command': command, 'exit': result.returncode})
    (lane / 'post-contract-receipts.json').write_text(json.dumps(receipts, indent=2) + '\n')
    print(f'EXIT {name} {result.returncode}', flush=True)
subprocess.run([python, str(lane / 'freeze_tree.py'), str(lane / 'post-contract-after.json')], env=env, check=True)
frozen = (lane / 'post-contract-before.json').read_bytes() == (lane / 'post-contract-after.json').read_bytes()
(lane / 'post-contract-frozen.txt').write_text(f'frozen={frozen}\n')
print(f'FROZEN {frozen}', flush=True)
# Preserve the raw boundary scanner's nonzero exit even when its known corpus
# is unchanged. The independent delta receipt records whether D2 adds a finding.
sys.exit(0 if frozen and all(receipt['exit'] == 0 for receipt in receipts) else 1)
