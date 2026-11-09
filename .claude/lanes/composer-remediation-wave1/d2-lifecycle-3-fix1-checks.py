from pathlib import Path
import json,os,subprocess,time
root=Path('/workspace/elspeth/.claude/worktrees/composer-remediation-mcp-lifecycle')
lane=Path('/workspace/elspeth/.claude/lanes/composer-remediation-wave1')
python=str(root/'.venv/bin/python')
env=dict(os.environ,PYTHONPATH=f'{root}/src:{root}/elspeth-lints/src')
paths=['src/elspeth/composer_mcp/portable_supervisor.py','tests/helpers/composer_stdio_ownership_probe.py','tests/unit/composer_mcp/test_standalone_lifecycle.py']
commands=[('imports',[python,'-c','import elspeth,elspeth_lints,sys; print(elspeth.__file__); print(elspeth_lints.__file__); print(sys.version); print(sys.platform)']),('before',[python,str(lane/'d2-checks/freeze_tree.py'),str(lane/'d2-lifecycle-3-fix1-before.json')]),('pytest',[python,'-m','pytest','tests/unit/composer_mcp/test_standalone_lifecycle.py','tests/unit/composer_mcp/test_portable_supervisor.py','-n','0','-o',f'pythonpath={root}/src {root}/elspeth-lints/src','-k','not production_grace and not blocked_actual_preflight',f'--basetemp={lane}/d2-lifecycle-3-fix1-pytest',f'--junitxml={lane}/d2-lifecycle-3-fix1.xml']),('ruff',[python,'-m','ruff','check',*paths]),('format',[python,'-m','ruff','format','--check',*paths]),('mypy',[python,'-m','mypy','--cache-dir',str(root/'.claude/lifecycle-mypy'),'src/elspeth/composer_mcp/portable_supervisor.py']),('contracts',[python,'-m','scripts.check_contracts']),('whitespace',['git','diff','--check']),('after',[python,str(lane/'d2-checks/freeze_tree.py'),str(lane/'d2-lifecycle-3-fix1-after.json')])]
receipts=[]
for name,command in commands:
    start=time.monotonic()
    with (lane/f'd2-lifecycle-3-fix1-{name}.log').open('w') as output:
        result=subprocess.run(command,cwd=root,env=env,stdout=output,stderr=subprocess.STDOUT,timeout=300)
    (lane/f'd2-lifecycle-3-fix1-{name}.exit').write_text(str(result.returncode)+'\n')
    receipts.append(dict(name=name,command=command,cwd=str(root),PYTHONPATH=env['PYTHONPATH'],returncode=result.returncode,elapsed_seconds=time.monotonic()-start))
    (lane/'d2-lifecycle-3-fix1-receipts.json').write_text(json.dumps(receipts,indent=2))
    print(name,result.returncode,flush=True)
frozen=(lane/'d2-lifecycle-3-fix1-before.json').read_bytes()==(lane/'d2-lifecycle-3-fix1-after.json').read_bytes()
print('frozen',frozen,flush=True)
raise SystemExit(0 if frozen and all(x['returncode']==0 for x in receipts) else 1)
