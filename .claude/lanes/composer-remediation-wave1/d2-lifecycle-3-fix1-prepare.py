from pathlib import Path
import hashlib,json,os,subprocess
root=Path('/workspace/elspeth/.claude/worktrees/composer-remediation-mcp-lifecycle')
lane=Path('/workspace/elspeth/.claude/lanes/composer-remediation-wave1')
artifact=lane/'d2-lifecycle-3-review.diff'
assert hashlib.sha256(artifact.read_bytes()).hexdigest()=='fa0297dbf92d5faeb6a80099d28b216daa7fb282b1c1eaa25b66e1a19c629949'
snapshot=lane/'d2-lifecycle-3-fix1-reviewed-source'
snapshot.mkdir(exist_ok=False)
names=[line.split('\t',2)[2] for line in subprocess.check_output(['git','apply','--numstat',str(artifact)],cwd=root,text=True).splitlines()]
for name in names:
    result=subprocess.run(['git','show',f'HEAD:{name}'],cwd=root,capture_output=True)
    if result.returncode==0:
        path=snapshot/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(result.stdout)
subprocess.run(['patch','--batch','-p1','-d',str(snapshot),'-i',str(artifact)],check=True)
current=subprocess.check_output(['git','diff','--name-only','HEAD'],cwd=root,text=True).splitlines()+subprocess.check_output(['git','ls-files','--others','--exclude-standard'],cwd=root,text=True).splitlines()
changes=[name for name in sorted(set(names)|set(current)) if not (snapshot/name).exists() or not (root/name).exists() or (snapshot/name).read_bytes()!=(root/name).read_bytes()]
assert changes==['tests/helpers/composer_stdio_ownership_probe.py','tests/unit/composer_mcp/test_standalone_lifecycle.py'],changes
print(json.dumps({'original_sha256':hashlib.sha256(artifact.read_bytes()).hexdigest(),'baseline_delta':changes},indent=2))
subprocess.run([str(root/'.venv/bin/python'),str(lane/'d2-checks/freeze_tree.py'),str(lane/'d2-lifecycle-3-fix1-resume-before.json')],cwd=root,check=True)
