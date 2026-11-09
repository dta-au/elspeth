from pathlib import Path
import hashlib,json,os,subprocess,xml.etree.ElementTree as ET
root=Path('/workspace/elspeth/.claude/worktrees/composer-remediation-mcp-lifecycle')
lane=Path('/workspace/elspeth/.claude/lanes/composer-remediation-wave1')
expected={'src/elspeth/composer_mcp/__init__.py','src/elspeth/composer_mcp/portable_supervisor.py','src/elspeth/composer_mcp/server.py','src/elspeth/web/async_workers.py','src/elspeth/web/required_executor.py','tests/conftest.py','tests/fixtures/required_executor.py','tests/unit/composer_mcp/test_server.py','docs/guides/composer-mcp-lifecycle.md','src/elspeth/composer_mcp/runner.py','src/elspeth/composer_mcp/standalone.py','tests/helpers/composer_lifecycle_probe.py','tests/helpers/composer_parent_death_probe.py','tests/helpers/composer_stdio_probe.py','tests/unit/composer_mcp/test_standalone_lifecycle.py','tests/unit/web/test_async_worker_settlement.py'}
def git(*args):
    return subprocess.check_output(['git',*args],cwd=root)
tracked=git('diff','--name-only','HEAD').decode().splitlines()
new=git('ls-files','--others','--exclude-standard').decode().splitlines()
assert set(tracked+new)==expected,(tracked,new)
payload=git('diff','--binary','HEAD')
for name in new:
    process=subprocess.run(['git','diff','--no-index','--binary','--','/dev/null',name],cwd=root,capture_output=True)
    assert process.returncode==1,(name,process.stderr)
    payload+=process.stdout
artifact=lane/'d2-lifecycle-3-review.diff'
artifact.write_bytes(payload)
commands=[['git','diff','--check'],['git','apply','--reverse','--check',str(artifact)]]
receipts=[]
for index,command in enumerate(commands):
    result=subprocess.run(command,cwd=root,capture_output=True,text=True)
    (lane/f'd2-lifecycle-3-artifact-check-{index}.log').write_text(result.stdout+result.stderr)
    receipts.append(dict(command=command,returncode=result.returncode))
    assert result.returncode==0,receipts[-1]
numstat=git('apply','--numstat',str(artifact)).decode()
(lane/'d2-lifecycle-3-review-numstat.txt').write_text(numstat)
assert {line.split('\t',2)[2] for line in numstat.splitlines()}==expected
freeze_command=[str(root/'.venv/bin/python'),str(lane/'d2-checks/freeze_tree.py'),str(lane/'d2-lifecycle-3-review-freeze.json')]
freeze=subprocess.run(freeze_command,cwd=root,env=dict(os.environ,PYTHONPATH=f'{root}/src:{root}/elspeth-lints/src'),capture_output=True,text=True)
(lane/'d2-lifecycle-3-review-freeze.log').write_text(freeze.stdout+freeze.stderr)
assert freeze.returncode==0
assert (lane/'d2-lifecycle-3-review-freeze.json').read_bytes()==(lane/'d2-lifecycle-3-supplement-after.json').read_bytes()
metadata=dict(head=git('rev-parse','HEAD').decode().strip(),artifact=str(artifact),bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest(),tracked=tracked,new=new,checks=receipts,freeze_command=freeze_command,freeze_returncode=freeze.returncode,freeze=json.loads(freeze.stdout))
(lane/'d2-lifecycle-3-artifact.json').write_text(json.dumps(metadata,indent=2))
(lane/'d2-lifecycle-3-final-status.txt').write_bytes(git('status','--short'))
public_root=lane/'d2-lifecycle-3-supplement-pytest/test_public_executable_real_pr0'
public=json.loads((public_root/'receipt.json').read_text())
proofs={'public':{'returncode':public['returncode'],'topology':public['topology'],'original_preview_valid':public['preview']['data']['preview_is_valid'],'singular_file_valid':public['source_preview']['data']['preview_is_valid'],'plural_file_valid':public['sources_preview']['data']['preview_is_valid'],'legacy_valid':public['legacy_preview']['data']['preview_is_valid'],'legacy_failed_checks':[check['name'] for check in public['legacy_preview']['runtime_preflight']['checks'] if not check['passed']],'provider_author_success':public['provider_author']['success'],'provider_export_success':public['provider_export']['success'],'provider_canaries_absent_from_export':all(value not in json.dumps(public['provider_export']) for value in ['PRIVATE-SOURCE.csv','PRIVATE-OUTPUT.csv','PRIVATE-PROVIDER'])},'processes':{}}
final=lane/'d2-lifecycle-3-final-pytest'
for path in sorted(final.rglob('status.json')):
    if path.parent.name.startswith(('test_real_partial_startup_tran','test_blocked_actual_preflight_')):
        proofs['processes'][str(path.relative_to(lane))]=json.loads(path.read_text())
for filename in ('parent-death.json','channel-loss.json'):
    for path in final.rglob(filename):
        proofs[filename]=dict(path=str(path),result=json.loads(path.read_text()))
for path in (lane/'d2-lifecycle-3-supplement-pytest').rglob('status.json'):
    proofs['timeout_control']=dict(path=str(path),result=json.loads(path.read_text()))
proofs['deadline_cases']=[case.attrib for case in ET.parse(lane/'d2-lifecycle-3-final.xml').iter('testcase') if 'real-deadline-kills' in case.attrib['name']]
legacy_checks=public['legacy_preview']['runtime_preflight']['checks']
del proofs['public']['legacy_failed_checks']
proofs['public']['legacy_actual_failed_checks']=[check['name'] for check in legacy_checks if not check['passed'] and check['outcome_code']!='validation.skipped_after_failure']
proofs['public']['legacy_skipped_checks']=[check['name'] for check in legacy_checks if check['outcome_code']=='validation.skipped_after_failure']
(lane/'d2-lifecycle-3-process-proofs.json').write_text(json.dumps(proofs,indent=2))
print(json.dumps(metadata,indent=2))
print(json.dumps(proofs,indent=2))
