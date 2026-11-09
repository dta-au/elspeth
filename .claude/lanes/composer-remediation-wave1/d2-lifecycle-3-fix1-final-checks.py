from pathlib import Path
import hashlib,json,os,subprocess,time
root=Path('/workspace/elspeth/.claude/worktrees/composer-remediation-mcp-lifecycle')
lane=Path('/workspace/elspeth/.claude/lanes/composer-remediation-wave1')
python=str(root/'.venv/bin/python')
env=dict(os.environ,PYTHONPATH=f'{root}/src:{root}/elspeth-lints/src')
paths=['src/elspeth/composer_mcp/portable_supervisor.py','tests/helpers/composer_stdio_ownership_probe.py','tests/unit/composer_mcp/test_standalone_lifecycle.py']
commands=[('before',[python,str(lane/'d2-checks/freeze_tree.py'),str(lane/'d2-lifecycle-3-fix1-final-before.json')]),('pytest',[python,'-m','pytest','tests/unit/composer_mcp/test_standalone_lifecycle.py::test_dedicated_supervisor_releases_actual_transport_ends_while_alive','-n','0','-o',f'pythonpath={root}/src {root}/elspeth-lints/src',f'--basetemp={lane}/d2-lifecycle-3-fix1-final-pytest',f'--junitxml={lane}/d2-lifecycle-3-fix1-final.xml']),('ruff',[python,'-m','ruff','check',*paths]),('format',[python,'-m','ruff','format','--check',*paths]),('whitespace',['git','diff','--check']),('after',[python,str(lane/'d2-checks/freeze_tree.py'),str(lane/'d2-lifecycle-3-fix1-final-after.json')])]
receipts=[]
for name,command in commands:
    start=time.monotonic()
    with (lane/f'd2-lifecycle-3-fix1-final-{name}.log').open('w') as output:
        result=subprocess.run(command,cwd=root,env=env,stdout=output,stderr=subprocess.STDOUT,timeout=40)
    (lane/f'd2-lifecycle-3-fix1-final-{name}.exit').write_text(str(result.returncode)+'\n')
    receipts.append(dict(name=name,command=command,cwd=str(root),PYTHONPATH=env['PYTHONPATH'],returncode=result.returncode,elapsed_seconds=time.monotonic()-start))
    (lane/'d2-lifecycle-3-fix1-final-receipts.json').write_text(json.dumps(receipts,indent=2))
    print(name,result.returncode,flush=True)
before=(lane/'d2-lifecycle-3-fix1-before.json').read_bytes()
after=(lane/'d2-lifecycle-3-fix1-after.json').read_bytes()
final_before=(lane/'d2-lifecycle-3-fix1-final-before.json').read_bytes()
final_after=(lane/'d2-lifecycle-3-fix1-final-after.json').read_bytes()
previous={x['path']:x for x in json.loads(after)['entries']}
current={x['path']:x for x in json.loads(final_after)['entries']}
changes=[name for name in sorted(previous.keys()|current.keys()) if previous.get(name)!=current.get(name)]
metadata=dict(cohort_frozen=before==after,final_frozen=final_before==final_after,delta_after_cohort=changes,production_unchanged=changes==['tests/helpers/composer_stdio_ownership_probe.py'],cohort_sha256=hashlib.sha256(before.rstrip(b'\n')).hexdigest(),final_sha256=hashlib.sha256(final_after.rstrip(b'\n')).hexdigest(),inputs=len(current))
(lane/'d2-lifecycle-3-fix1-freeze-comparison.json').write_text(json.dumps(metadata,indent=2))
print(json.dumps(metadata,indent=2),flush=True)
raise SystemExit(0 if metadata['cohort_frozen'] and metadata['final_frozen'] and metadata['production_unchanged'] and all(x['returncode']==0 for x in receipts) else 1)
