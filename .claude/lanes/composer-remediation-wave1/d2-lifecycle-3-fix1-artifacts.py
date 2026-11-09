from pathlib import Path
import hashlib,json,subprocess
root=Path('/workspace/elspeth/.claude/worktrees/composer-remediation-mcp-lifecycle')
lane=Path('/workspace/elspeth/.claude/lanes/composer-remediation-wave1')
original=lane/'d2-lifecycle-3-review.diff'
assert hashlib.sha256(original.read_bytes()).hexdigest()=='fa0297dbf92d5faeb6a80099d28b216daa7fb282b1c1eaa25b66e1a19c629949'
snapshot=lane/'d2-lifecycle-3-fix1-reviewed-source'
expected=['src/elspeth/composer_mcp/portable_supervisor.py','tests/helpers/composer_stdio_ownership_probe.py','tests/unit/composer_mcp/test_standalone_lifecycle.py']
def git(*args):
    return subprocess.check_output(['git',*args],cwd=root)
tracked=git('diff','--name-only','HEAD').decode().splitlines()
new=git('ls-files','--others','--exclude-standard').decode().splitlines()
changes=[name for name in sorted(set(tracked+new)) if not (snapshot/name).exists() or (snapshot/name).read_bytes()!=(root/name).read_bytes()]
assert changes==expected,changes
fix=b''
for name in changes:
    old=str(snapshot/name) if (snapshot/name).exists() else '/dev/null'
    fresh=str(root/name)
    result=subprocess.run(['git','diff','--no-index','--binary','--',old,fresh],cwd=root,capture_output=True)
    assert result.returncode==1,(name,result.stderr)
    diff=result.stdout
    if old=='/dev/null':
        diff=diff.replace(('a'+fresh).encode(),('a/'+name).encode())
    else:
        diff=diff.replace(('a'+old).encode(),('a/'+name).encode())
    diff=diff.replace(('b'+fresh).encode(),('b/'+name).encode())
    fix+=diff
full=git('diff','--binary','HEAD')
for name in new:
    result=subprocess.run(['git','diff','--no-index','--binary','--','/dev/null',name],cwd=root,capture_output=True)
    assert result.returncode==1
    full+=result.stdout
metadata={'head':git('rev-parse','HEAD').decode().strip(),'fix_paths':changes,'tracked':tracked,'new':new,'artifacts':[]}
for suffix,data in [('fix1',fix),('fix1-full',full)]:
    artifact=lane/f'd2-lifecycle-3-{suffix}.diff'
    artifact.write_bytes(data)
    checks=[]
    for args in [('apply','--reverse','--check',str(artifact)),('apply','--check','--whitespace=error-all','--reverse',str(artifact))]:
        result=subprocess.run(['git',*args],cwd=root,capture_output=True,text=True)
        checks.append({'command':['git',*args],'exit':result.returncode,'stdout':result.stdout,'stderr':result.stderr})
        assert result.returncode==0,checks[-1]
    numstat=git('apply','--numstat',str(artifact)).decode()
    (lane/f'd2-lifecycle-3-{suffix}-numstat.txt').write_text(numstat)
    metadata['artifacts'].append({'path':str(artifact),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'checks':checks,'numstat':numstat})
(lane/'d2-lifecycle-3-fix1-artifacts.json').write_text(json.dumps(metadata,indent=2))
print(json.dumps(metadata,indent=2))
