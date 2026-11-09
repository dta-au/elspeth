from pathlib import Path
import json
import os
import subprocess
import time

ROOT = Path('/workspace/elspeth/.claude/worktrees/composer-remediation-mcp-lifecycle')
LANE = Path('/workspace/elspeth/.claude/lanes/composer-remediation-wave1')
PYTHON = str(ROOT / '.venv/bin/python')
ENV = dict(os.environ, PYTHONPATH=f'{ROOT}/src:{ROOT}/elspeth-lints/src')
FREEZE = str(LANE / 'd2-checks/freeze_tree.py')
PRODUCTION = ['src/elspeth/composer_mcp/__init__.py', 'src/elspeth/composer_mcp/server.py', 'src/elspeth/composer_mcp/runner.py', 'src/elspeth/composer_mcp/standalone.py', 'src/elspeth/composer_mcp/portable_supervisor.py', 'src/elspeth/web/async_workers.py', 'src/elspeth/web/required_executor.py']
PYFILES = PRODUCTION + ['tests/conftest.py', 'tests/fixtures/required_executor.py', 'tests/unit/composer_mcp/test_server.py', 'tests/unit/composer_mcp/test_standalone_lifecycle.py', 'tests/unit/web/test_async_worker_settlement.py', 'tests/helpers/composer_lifecycle_probe.py', 'tests/helpers/composer_stdio_probe.py', 'tests/helpers/composer_parent_death_probe.py']
receipts = []

def run(name, command, timeout=600):
    started = time.monotonic()
    log = LANE / f'd2-lifecycle-3-final-{name}.log'
    with log.open('w') as stream:
        process = subprocess.run(command, cwd=ROOT, env=ENV, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
    elapsed = time.monotonic() - started
    (LANE / f'd2-lifecycle-3-final-{name}.exit').write_text(str(process.returncode) + '\n')
    receipts.append({'name': name, 'command': command, 'cwd': str(ROOT), 'PYTHONPATH': ENV['PYTHONPATH'], 'returncode': process.returncode, 'elapsed_seconds': elapsed, 'log': str(log)})
    (LANE / 'd2-lifecycle-3-final-receipts.json').write_text(json.dumps(receipts, indent=2))
    print(f'{name}: exit {process.returncode}, {elapsed:.2f}s', flush=True)
    return process.returncode

run('imports', [PYTHON, '-c', 'import elspeth, elspeth_lints, os, sys; print(elspeth.__file__); print(elspeth_lints.__file__); print(sys.version); print(sys.platform, os.name)'])
run('before', [PYTHON, FREEZE, str(LANE / 'd2-lifecycle-3-freeze-before.json')])
run('pytest', [PYTHON, '-m', 'pytest', 'tests/unit/composer_mcp', 'tests/unit/web/execution/test_runtime_preflight_coordinator.py', 'tests/unit/web/test_process_watchdog.py', 'tests/unit/web/test_process_recovery.py', 'tests/unit/web/test_async_worker_settlement.py', 'tests/unit/web/test_required_executor_custody.py', 'tests/unit/web/test_required_executor_factory.py', 'tests/unit/web/test_required_executor_completion_identity.py', 'tests/unit/web/test_async_workers.py', 'tests/unit/web/test_paths.py', '-n', '0', '-o', f'pythonpath={ROOT}/src {ROOT}/elspeth-lints/src', '-k', 'not production_grace and not real_watchdog_armed_channel_loss', f'--basetemp={LANE}/d2-lifecycle-3-final-pytest', f'--junitxml={LANE}/d2-lifecycle-3-final.xml'], timeout=900)
run('ruff', [PYTHON, '-m', 'ruff', 'check', *PYFILES])
run('format', [PYTHON, '-m', 'ruff', 'format', '--check', *PYFILES])
run('mypy', [PYTHON, '-m', 'mypy', '--cache-dir', str(ROOT / '.claude/lifecycle-mypy'), *PRODUCTION])
run('contracts', [PYTHON, '-m', 'scripts.check_contracts'])
run('whitespace', ['git', 'diff', '--check'])
run('after', [PYTHON, FREEZE, str(LANE / 'd2-lifecycle-3-freeze-after.json')])
before = (LANE / 'd2-lifecycle-3-freeze-before.json').read_bytes()
after = (LANE / 'd2-lifecycle-3-freeze-after.json').read_bytes()
(LANE / 'd2-lifecycle-3-frozen.txt').write_text(f'frozen={before == after}\n')
print(f'frozen={before == after}', flush=True)
raise SystemExit(0 if before == after and all(receipt['returncode'] == 0 for receipt in receipts) else 1)
