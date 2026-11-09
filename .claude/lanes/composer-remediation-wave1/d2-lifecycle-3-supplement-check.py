from pathlib import Path
import json,os,subprocess,time
root=Path('/workspace/elspeth/.claude/worktrees/composer-remediation-mcp-lifecycle')
lane=Path('/workspace/elspeth/.claude/lanes/composer-remediation-wave1')
python=str(root/'.venv/bin/python')
env=dict(os.environ,PYTHONPATH=f'{root}/src:{root}/elspeth-lints/src')
receipts=[]
commands=[('before',[python,str(lane/'d2-checks/freeze_tree.py'),str(lane/'d2-lifecycle-3-supplement-before.json')]),('pytest',[python,'-m','pytest','tests/unit/composer_mcp/test_standalone_lifecycle.py::test_public_executable_real_preview_paths_and_eof','tests/unit/composer_mcp/test_standalone_lifecycle.py::test_blocked_actual_preflight_retains_admission_until_join_or_deadline[release-joins]','-n','0','-o',f'pythonpath={root}/src {root}/elspeth-lints/src',f'--basetemp={lane}/d2-lifecycle-3-supplement-pytest',f'--junitxml={lane}/d2-lifecycle-3-supplement.xml']),('ruff',[python,'-m','ruff','check','tests/helpers/composer_stdio_probe.py','tests/unit/composer_mcp/test_standalone_lifecycle.py']),('format',[python,'-m','ruff','format','--check','tests/helpers/composer_stdio_probe.py','tests/unit/composer_mcp/test_standalone_lifecycle.py']),('whitespace',['git','diff','--check']),('after',[python,str(lane/'d2-checks/freeze_tree.py'),str(lane/'d2-lifecycle-3-supplement-after.json')])]
for name,command in commands:
    started=time.monotonic()
    with (lane/f'd2-lifecycle-3-supplement-{name}.log').open('w') as log:
        process=subprocess.run(command,cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=90)
    (lane/f'd2-lifecycle-3-supplement-{name}.exit').write_text(str(process.returncode)+'\n')
    receipts.append(dict(name=name,command=command,cwd=str(root),PYTHONPATH=env['PYTHONPATH'],returncode=process.returncode,elapsed_seconds=time.monotonic()-started))
    (lane/'d2-lifecycle-3-supplement-receipts.json').write_text(json.dumps(receipts,indent=2))
    print(name,process.returncode,flush=True)
before=(lane/'d2-lifecycle-3-supplement-before.json').read_bytes()
after=(lane/'d2-lifecycle-3-supplement-after.json').read_bytes()
old=json.loads((lane/'d2-lifecycle-3-freeze-after.json').read_text())
new=json.loads(after)
old_entries={entry['path']:entry for entry in old['entries']}
new_entries={entry['path']:entry for entry in new['entries']}
changed=[name for name in sorted(old_entries.keys()|new_entries.keys()) if old_entries.get(name)!=new_entries.get(name)]
expected=['tests/helpers/composer_stdio_probe.py','tests/unit/composer_mcp/test_standalone_lifecycle.py']
comparison=dict(frozen=before==after,changed_since_600=changed,production_unchanged=changed==expected)
(lane/'d2-lifecycle-3-supplement-freeze.json').write_text(json.dumps(comparison,indent=2))
print(comparison,flush=True)
raise SystemExit(0 if before==after and changed==expected and all(item['returncode']==0 for item in receipts) else 1)
